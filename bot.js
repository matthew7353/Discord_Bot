require('dotenv').config();
const { Client, GatewayIntentBits, AttachmentBuilder } = require('discord.js');
const { joinVoiceChannel, EndBehaviorType } = require('@discordjs/voice');
const { BlobServiceClient } = require('@azure/storage-blob');
const { ManagedIdentityCredential } = require('@azure/identity');
const { ContainerAppsAPIClient } = require('@azure/arm-appcontainers');
const fs = require('fs');
const path = require('path');
const prism = require('prism-media');

// --- AZURE STORAGE CONFIG ---
// Ten sam kontener co AZURE_STORAGE_CONTAINER w jobie (orchestrator.py).
const AZURE_CONNECTION_STRING = process.env.AZURE_STORAGE_CONNECTION_STRING;
const containerName = process.env.AZURE_STORAGE_CONTAINER || 'audio-files';
const blobServiceClient = BlobServiceClient.fromConnectionString(AZURE_CONNECTION_STRING);
const containerClient = blobServiceClient.getContainerClient(containerName);

// --- AZURE CONTAINER APPS JOB (PROCESSING) CONFIG ---
// Przetwarzanie (Whisper + Ollama) robi Azure Container Apps Job z brancha azure-job.
// Bot uruchamia jego wykonanie przez ARM, używając System-Assigned Managed Identity
// tej maszyny (rola z uprawnieniem Microsoft.App/jobs/start/action oraz
// Microsoft.App/jobs/executions/read na jobie, np. "Contributor" scoped na job) — zero sekretów w kodzie.
const AZURE_SUBSCRIPTION_ID = process.env.AZURE_SUBSCRIPTION_ID;
const AZURE_RESOURCE_GROUP = process.env.AZURE_RESOURCE_GROUP;
const AZURE_PROCESSING_JOB_NAME = process.env.AZURE_PROCESSING_JOB_NAME;

let jobsClient = null;
if (AZURE_SUBSCRIPTION_ID && AZURE_RESOURCE_GROUP && AZURE_PROCESSING_JOB_NAME) {
    const credential = new ManagedIdentityCredential();
    jobsClient = new ContainerAppsAPIClient(credential, AZURE_SUBSCRIPTION_ID);
} else {
    console.warn('[AZURE JOB] ⚠️ Brak AZURE_SUBSCRIPTION_ID/AZURE_RESOURCE_GROUP/AZURE_PROCESSING_JOB_NAME — auto-start joba przetwarzającego wyłączony.');
}

// Poll statusu co 20s, poddajemy się po 12h (job ma replica-timeout 4h,
// ale może być uruchamiany ponownie, jeśli wykonanie padnie).
const POLL_INTERVAL_MS = 20_000;
const POLL_TIMEOUT_MS = 12 * 60 * 60 * 1000;
// Co ile sprawdzać, czy job nadal działa, gdy sesja wciąż czeka na przetworzenie.
const JOB_CHECK_INTERVAL_MS = 5 * 60 * 1000;
// Ile razy maksymalnie bot sam wznowi job dla jednej sesji.
const MAX_JOB_RESTARTS = 2;

// Statusy z orchestrator.py, przy których sesja nadal czeka na job.
const PENDING_STATUSES = ['recorded', 'processing', 'transcribed'];
// Statusy wykonania joba oznaczające, że wciąż działa.
const ACTIVE_EXECUTION_STATES = ['Running', 'Processing'];

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
let uploadedFiles = 0;
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

// ---------- Job control ----------

async function isProcessingJobActive() {
    for await (const execution of jobsClient.jobsExecutions.list(AZURE_RESOURCE_GROUP, AZURE_PROCESSING_JOB_NAME)) {
        if (ACTIVE_EXECUTION_STATES.includes(execution.status)) return true;
    }
    return false;
}

// Zwraca true, jeśli job działa (już działał albo właśnie został uruchomiony).
async function startProcessingJob() {
    if (!jobsClient) {
        console.warn('[AZURE JOB] Auto-start pominięty (brak konfiguracji).');
        return false;
    }
    try {
        // Działające wykonanie samo podejmie nową sesję (orchestrator kilka razy
        // skanuje blob przed zakończeniem) — nie odpalamy drugiego na GPU.
        if (await isProcessingJobActive()) {
            console.log('[AZURE JOB] Job przetwarzający już działa — pomijam start.');
            return true;
        }
        console.log('[AZURE JOB] 🚀 Uruchamiam job przetwarzający...');
        // Nie czekamy na zakończenie long-running operation — wystarczy, że Azure przyjął start.
        const poller = await jobsClient.jobs.beginStart(AZURE_RESOURCE_GROUP, AZURE_PROCESSING_JOB_NAME);
        poller.pollUntilDone()
            .then((exec) => console.log(`[AZURE JOB] Wykonanie uruchomione: ${exec?.name || '?'}`))
            .catch(err => console.error(`[AZURE JOB BŁĄD] ${err.message}`));
        return true;
    } catch (error) {
        console.error(`[AZURE JOB BŁĄD] Nie udało się sprawdzić/uruchomić joba: ${error.message}`);
        return false;
    }
}

// ---------- Session lifecycle ----------

async function finalizeSession(sessionId, channel, uploadedFiles) {
    try {
        // Bez żadnego nagrania orchestrator i tak oznaczy sesję jako "error"
        // ("Brak plików .pcm") — nie ma sensu płacić za start GPU.
        if (uploadedFiles === 0) {
            await channel.send('🔇 Żadne nagranie z tej sesji nie trafiło do chmury — pomijam transkrypcję.');
            return;
        }
        // status.json w formacie oczekiwanym przez orchestrator.py
        // (find_sessions: status "recorded", sortowanie po created_at).
        const now = new Date().toISOString();
        await writeSessionStatus(sessionId, {
            session_id: sessionId,
            status: 'recorded',
            created_at: now,
            updated_at: now,
        });
        await channel.send('📼 Nagranie wysłane do chmury. Uruchamiam job do transkrypcji i notatek — to może potrwać chwilę...');
        const started = await startProcessingJob();
        if (!started) {
            await channel.send('⚠️ Nie udało się uruchomić joba przetwarzającego — sesja czeka w kolejce, uruchom job ręcznie.');
        }
        pollSessionCompletion(sessionId, channel);
    } catch (err) {
        console.error(`[SESSION BŁĄD] ${err.message}`);
        await channel.send(`⚠️ Błąd przy finalizowaniu sesji: ${err.message}`)
            .catch(e => console.error(`[DISCORD BŁĄD] ${e.message}`));
    }
}

function pollSessionCompletion(sessionId, channel) {
    const startedAt = Date.now();
    let lastJobCheck = Date.now();
    let jobRestarts = 0;
    let announcedTranscribed = false;
    let busy = false;

    const interval = setInterval(async () => {
        if (busy) return;
        busy = true;
        try {
            if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
                clearInterval(interval);
                const hours = Math.round(POLL_TIMEOUT_MS / 3_600_000);
                await channel.send(`⌛ Minęło ${hours}h bez wyniku — sprawdź job przetwarzający ręcznie.`);
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
                return;
            }

            if (status.status === 'error') {
                clearInterval(interval);
                await channel.send(`❌ Błąd podczas przetwarzania sesji: ${status.error || 'nieznany błąd'}`);
                return;
            }

            if (status.status === 'transcribed' && !announcedTranscribed) {
                announcedTranscribed = true;
                await channel.send('📝 Transkrypcja gotowa, generuję notatki...');
            }

            // Sesja wciąż czeka, a job mógł się zakończyć bez niej (np. był w fazie
            // notatek, gdy sesja się pojawiła, albo wykonanie padło / przekroczyło
            // timeout). Orchestrator wznawia sesje "recorded"/"processing"/"transcribed",
            // więc wystarczy uruchomić job ponownie.
            if (jobsClient && PENDING_STATUSES.includes(status.status)
                && Date.now() - lastJobCheck > JOB_CHECK_INTERVAL_MS) {
                lastJobCheck = Date.now();
                if (!(await isProcessingJobActive())) {
                    if (jobRestarts >= MAX_JOB_RESTARTS) {
                        clearInterval(interval);
                        await channel.send(`⚠️ Job przetwarzający zakończył się, a sesja wciąż ma status "${status.status}". Sprawdź logi joba.`);
                        return;
                    }
                    jobRestarts += 1;
                    console.log(`[AZURE JOB] Sesja ${sessionId} wciąż "${status.status}", job nie działa — ponowny start (${jobRestarts}/${MAX_JOB_RESTARTS}).`);
                    await startProcessingJob();
                }
            }
        } catch (err) {
            console.error(`[POLL BŁĄD] ${err.message}`);
        } finally {
            busy = false;
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
        uploadedFiles = 0;
        stopRequested = false;

        currentConnection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: message.guild.id,
            adapterCreator: message.guild.voiceAdapterCreator,
            selfDeaf: false,
        });
        currentConnection.on('error', (err) => console.error(`[VOICE BŁĄD] ${err.message}`));

        message.reply(`🎤 Nasłuchuję na **${voiceChannel.name}**. (sesja: ${currentSessionId})`);

        const receiver = currentConnection.receiver;

        receiver.speaking.on('start', (userId) => {
            if (activeRecordings.has(userId)) {
                const existing = activeRecordings.get(userId);
                if (existing.stopTimeout) clearTimeout(existing.stopTimeout);
                return;
            }

            const user = client.users.cache.get(userId);
            // transcribe.py parsuje nazwę jako audio_<user>_<timestamp>.pcm (split po "_"),
            // więc nick nie może zawierać "_" ani znaków niebezpiecznych w ścieżce.
            const userName = (user ? user.username : userId).replace(/[^A-Za-z0-9.-]/g, '-');

            const sessionId = currentSessionId;
            const timestamp = Date.now();
            const localFilename = path.join(recordingsDir, `audio_${userName}_${timestamp}.pcm`);
            const blobName = `sessions/${sessionId}/audio_${userName}_${timestamp}.pcm`;

            console.log(`[START] 🎙️ Nagrywanie dla: ${userName}`);

            pendingUploads += 1;

            const fileStream = fs.createWriteStream(localFilename);
            // Bez tego handlera błąd zapisu (np. write after end) wywaliłby cały proces.
            fileStream.on('error', (err) => console.error(`[PLIK BŁĄD] ${path.basename(localFilename)}: ${err.message}`));

            fileStream.on('finish', async () => {
                try {
                    await uploadFileToAzure(localFilename, blobName);
                    uploadedFiles += 1;
                } catch (error) {
                    console.error(`[AZURE BŁĄD] ❌ Krytyczny błąd wysyłki: ${error.message}`);
                } finally {
                    pendingUploads -= 1;
                    if (stopRequested && pendingUploads === 0) {
                        finalizeSession(sessionId, sessionChannel, uploadedFiles);
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
                finalizeSession(sessionId, channel, uploadedFiles);
            }
        }
    }
});

// Bot ma działać bez nadzoru — pojedynczy błąd (np. brak uprawnień do pisania
// na kanale albo chwilowy problem z Azure) nie może zabić procesu.
client.on('error', (err) => console.error(`[DISCORD BŁĄD] ${err.message}`));
process.on('unhandledRejection', (reason) => {
    console.error(`[UNHANDLED REJECTION] ${reason?.stack || reason}`);
});

client.login(process.env.DISCORD_TOKEN);
