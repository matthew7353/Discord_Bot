require('dotenv').config();
const { Client, GatewayIntentBits, AttachmentBuilder } = require('discord.js');
const { joinVoiceChannel, EndBehaviorType } = require('@discordjs/voice');
const { BlobServiceClient } = require('@azure/storage-blob');
const { ManagedIdentityCredential } = require('@azure/identity');
const { ComputeManagementClient } = require('@azure/arm-compute');
const fs = require('fs');
const path = require('path');
const prism = require('prism-media');

// --- AZURE STORAGE CONFIG ---
const AZURE_CONNECTION_STRING = process.env.AZURE_STORAGE_CONNECTION_STRING;
const containerName = 'audio-files';
const blobServiceClient = BlobServiceClient.fromConnectionString(AZURE_CONNECTION_STRING);
const containerClient = blobServiceClient.getContainerClient(containerName);

// --- AZURE VM (PROCESSING BOX) CONFIG ---
// Ta maszyna też jest na Azure, więc zamiast service principal z sekretem
// używamy jej własnej System-Assigned Managed Identity (rola "Virtual Machine
// Contributor" scoped na maszynę przetwarzającą, patrz SETUP.md) — zero sekretów w kodzie.
const AZURE_SUBSCRIPTION_ID = process.env.AZURE_SUBSCRIPTION_ID;
const AZURE_RESOURCE_GROUP = process.env.AZURE_RESOURCE_GROUP;
const AZURE_PROCESSING_VM_NAME = process.env.AZURE_PROCESSING_VM_NAME;

let computeClient = null;
if (AZURE_SUBSCRIPTION_ID && AZURE_RESOURCE_GROUP && AZURE_PROCESSING_VM_NAME) {
    const credential = new ManagedIdentityCredential();
    computeClient = new ComputeManagementClient(credential, AZURE_SUBSCRIPTION_ID);
} else {
    console.warn('[AZURE VM] ⚠️ Brak AZURE_SUBSCRIPTION_ID/AZURE_RESOURCE_GROUP/AZURE_PROCESSING_VM_NAME — auto-start maszyny przetwarzającej wyłączony.');
}

// Poll every 20s, give up after 2 hours (in case something goes wrong on the VM side).
const POLL_INTERVAL_MS = 20_000;
const POLL_TIMEOUT_MS = 12 * 60 * 60 * 1000;

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildVoiceStates,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent
    ],
});

const activeRecordings = new Map();
let currentConnection = null;
const recordingsDir = path.join(__dirname, 'recordings');

// --- SESSION STATE ---
let currentSessionId = null;
let sessionChannel = null;
let pendingUploads = 0;
let stopRequested = false;

if (!fs.existsSync(recordingsDir)) fs.mkdirSync(recordingsDir, { recursive: true });

// ---------- Blob helpers ----------

async function uploadFileToAzure(filePath, blobName) {
    const blockBlobClient = containerClient.getBlockBlobClient(blobName);
    console.log(`[AZURE] ⬆️ Próba wysłania: ${blobName}`);
    const uploadBlobResponse = await blockBlobClient.uploadFile(filePath);
    console.log(`[AZURE] ✅ Wysłano! RequestId: ${uploadBlobResponse.requestId}`);
    if (fs.existsSync(filePath)) {
        fs.unlinkSync(filePath);
        console.log(`[LOCAL] 🗑️ Usunięto: ${path.basename(filePath)}`);
    }
}

async function writeSessionStatus(sessionId, data) {
    const blobName = `sessions/${sessionId}/status.json`;
    const blockBlobClient = containerClient.getBlockBlobClient(blobName);
    const content = Buffer.from(JSON.stringify(data, null, 2));
    await blockBlobClient.upload(content, content.length, {
        overwrite: true,
        blobHTTPHeaders: { blobContentType: 'application/json' },
    });
}

async function readSessionStatus(sessionId) {
    const blobName = `sessions/${sessionId}/status.json`;
    const blockBlobClient = containerClient.getBlockBlobClient(blobName);
    if (!(await blockBlobClient.exists())) return null;
    const buf = await blockBlobClient.downloadToBuffer();
    return JSON.parse(buf.toString('utf-8'));
}

async function downloadBlobToBuffer(blobName) {
    const blockBlobClient = containerClient.getBlockBlobClient(blobName);
    return blockBlobClient.downloadToBuffer();
}

// ---------- VM control ----------

async function startProcessingVM() {
    if (!computeClient) {
        console.warn('[AZURE VM] Auto-start pominięty (brak konfiguracji).');
        return;
    }
    try {
        const view = await computeClient.virtualMachines.instanceView(AZURE_RESOURCE_GROUP, AZURE_PROCESSING_VM_NAME);
        const powerState = view.statuses?.find(s => s.code?.startsWith('PowerState/'))?.code;
        if (powerState === 'PowerState/running') {
            console.log('[AZURE VM] Maszyna przetwarzająca już działa — pomijam start.');
            return;
        }
        console.log('[AZURE VM] 🚀 Uruchamiam maszynę przetwarzającą...');
        // Fire-and-forget: don't block the bot waiting for full boot.
        computeClient.virtualMachines.beginStart(AZURE_RESOURCE_GROUP, AZURE_PROCESSING_VM_NAME)
            .then(() => console.log('[AZURE VM] Polecenie startu przyjęte przez Azure.'))
            .catch(err => console.error(`[AZURE VM BŁĄD] ${err.message}`));
    } catch (error) {
        console.error(`[AZURE VM BŁĄD] Nie udało się sprawdzić/uruchomić maszyny: ${error.message}`);
    }
}

// ---------- Session lifecycle ----------

async function finalizeSession(sessionId, channel) {
    try {
        await writeSessionStatus(sessionId, {
            session_id: sessionId,
            status: 'recorded',
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
        });
        await channel.send('📼 Nagranie wysłane do chmury. Uruchamiam maszynę do transkrypcji i notatek — to może potrwać chwilę...');
        await startProcessingVM();
        pollSessionCompletion(sessionId, channel);
    } catch (err) {
        console.error(`[SESSION BŁĄD] ${err.message}`);
        await channel.send(`⚠️ Błąd przy finalizowaniu sesji: ${err.message}`);
    }
}

function pollSessionCompletion(sessionId, channel) {
    const startedAt = Date.now();
    const interval = setInterval(async () => {
        try {
            if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
                clearInterval(interval);
                await channel.send('⌛ Minęły 2 godziny bez wyniku — sprawdź maszynę przetwarzającą ręcznie.');
                return;
            }

            const status = await readSessionStatus(sessionId);
            if (!status) return;

            if (status.status === 'done') {
                clearInterval(interval);
                let notesBuffer = null;
                if (status.notes_blob) {
                    notesBuffer = await downloadBlobToBuffer(status.notes_blob);
                }
                if (notesBuffer) {
                    const attachment = new AttachmentBuilder(notesBuffer, { name: 'notatki_z_sesji.md' });
                    await channel.send({ content: '✅ Notatki z sesji gotowe!', files: [attachment] });
                } else {
                    await channel.send('✅ Sesja przetworzona, ale nie znaleziono pliku z notatkami.');
                }
            } else if (status.status === 'error') {
                clearInterval(interval);
                await channel.send(`❌ Błąd podczas przetwarzania sesji: ${status.error || 'nieznany błąd'}`);
            }
        } catch (err) {
            console.error(`[POLL BŁĄD] ${err.message}`);
        }
    }, POLL_INTERVAL_MS);
}

// ---------- Discord events ----------

client.once('ready', () => {
    console.log(`✅ Zalogowano jako: ${client.user.tag}`);
    console.log(`☁️ Azure gotowy.`);
});

client.on('messageCreate', async (message) => {
    if (!message.guild || message.author.bot) return;

    if (message.content === '!start' || message.content === '!record') {
        const voiceChannel = message.member.voice.channel;
        if (!voiceChannel) return message.reply('Wejdź na kanał głosowy!');

        currentSessionId = Date.now().toString();
        sessionChannel = message.channel;
        pendingUploads = 0;
        stopRequested = false;

        currentConnection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: message.guild.id,
            adapterCreator: message.guild.voiceAdapterCreator,
            selfDeaf: false,
        });

        message.reply(`🎤 Nasłuchuję na **${voiceChannel.name}**. (sesja: ${currentSessionId})`);

        const receiver = currentConnection.receiver;

        receiver.speaking.on('start', (userId) => {
            if (activeRecordings.has(userId)) {
                const existing = activeRecordings.get(userId);
                if (existing.stopTimeout) clearTimeout(existing.stopTimeout);
                return;
            }

            const user = client.users.cache.get(userId);
            const userName = user ? user.username : userId;

            const sessionId = currentSessionId;
            const timestamp = Date.now();
            const localFilename = path.join(recordingsDir, `audio_${userName}_${timestamp}.pcm`);
            const blobName = `sessions/${sessionId}/audio_${userName}_${timestamp}.pcm`;

            console.log(`[START] 🎙️ Nagrywanie dla: ${userName}`);

            pendingUploads += 1;

            const fileStream = fs.createWriteStream(localFilename);

            fileStream.on('finish', async () => {
                try {
                    await uploadFileToAzure(localFilename, blobName);
                } catch (error) {
                    console.error(`[AZURE BŁĄD] ❌ Krytyczny błąd wysyłki: ${error.message}`);
                } finally {
                    pendingUploads -= 1;
                    if (stopRequested && pendingUploads === 0) {
                        finalizeSession(sessionId, sessionChannel);
                    }
                }
            });

            const audioStream = receiver.subscribe(userId, { end: { behavior: EndBehaviorType.Manual } });
            const opusDecoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });

            audioStream.on('error', (err) => console.error(`[Audio BŁĄD]: ${err.message}`));
            opusDecoder.on('error', (err) => console.error(`[Decoder BŁĄD]: ${err.message}`));

            audioStream.pipe(opusDecoder).pipe(fileStream);

            activeRecordings.set(userId, { audioStream, fileStream, userName, stopTimeout: null });
        });

        receiver.speaking.on('end', (userId) => {
            const rec = activeRecordings.get(userId);
            if (!rec) return;

            rec.stopTimeout = setTimeout(() => {
                console.log(`[CISZA] 🛑 3s u ${rec.userName} -> Zamykam.`);
                if (rec.audioStream && !rec.audioStream.destroyed) rec.audioStream.destroy();
                if (rec.fileStream) rec.fileStream.end();
                activeRecordings.delete(userId);
            }, 3000);
        });
    }

    if (message.content === '!stop') {
        if (currentConnection) {
            stopRequested = true;
            const sessionId = currentSessionId;
            const channel = sessionChannel;

            activeRecordings.forEach((rec) => {
                if (rec.stopTimeout) clearTimeout(rec.stopTimeout);
                if (rec.audioStream && !rec.audioStream.destroyed) rec.audioStream.destroy();
                if (rec.fileStream) rec.fileStream.end();
            });
            activeRecordings.clear();
            currentConnection.destroy();
            currentConnection = null;

            message.reply('⏹️ Rozłączono. Kończę wysyłkę plików...');

            // If every file already finished uploading, finalize immediately.
            // Otherwise the last fileStream 'finish' handler will do it.
            if (pendingUploads === 0) {
                finalizeSession(sessionId, channel);
            }
        }
    }
});

client.login(process.env.DISCORD_TOKEN);
