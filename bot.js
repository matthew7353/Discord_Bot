require('dotenv').config();
const { Client, GatewayIntentBits } = require('discord.js');
const { joinVoiceChannel, EndBehaviorType, VoiceConnectionStatus } = require('@discordjs/voice');
const { BlobServiceClient } = require('@azure/storage-blob');
const fs = require('fs');
const path = require('path');
const prism = require('prism-media');

// --- AZURE CONFIG ---
const AZURE_CONNECTION_STRING = process.env.AZURE_STORAGE_CONNECTION_STRING;
const containerName = 'audio-files'; 
const blobServiceClient = BlobServiceClient.fromConnectionString(AZURE_CONNECTION_STRING);
const containerClient = blobServiceClient.getContainerClient(containerName);

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

if (!fs.existsSync(recordingsDir)) fs.mkdirSync(recordingsDir, { recursive: true });

async function uploadToAzure(filePath) {
    try {
        const fileName = path.basename(filePath);
        const blockBlobClient = containerClient.getBlockBlobClient(fileName);
        
        console.log(`[AZURE] ⬆️ Próba wysłania: ${fileName}`);
        const uploadBlobResponse = await blockBlobClient.uploadFile(filePath);
        console.log(`[AZURE] ✅ Wysłano! RequestId: ${uploadBlobResponse.requestId}`);

        if (fs.existsSync(filePath)) {
            fs.unlinkSync(filePath);
            console.log(`[LOCAL] 🗑️ Usunięto: ${fileName}`);
        }
    } catch (error) {
        console.error(`[AZURE BŁĄD] ❌ Krytyczny błąd wysyłki: ${error.message}`);
    }
}

client.once('ready', () => {
    console.log(`✅ Zalogowano jako: ${client.user.tag}`);
    console.log(`☁️ Azure gotowy.`);
});

client.on('messageCreate', async (message) => {
    if (!message.guild || message.author.bot) return;

    if (message.content === '!start' || message.content === '!record') {
        const voiceChannel = message.member.voice.channel;
        if (!voiceChannel) return message.reply('Wejdź na kanał głosowy!');

        currentConnection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: message.guild.id,
            adapterCreator: message.guild.voiceAdapterCreator,
            selfDeaf: false,
        });

        message.reply(`🎤 Nasłuchuję na **${voiceChannel.name}**.`);

        const receiver = currentConnection.receiver;

        receiver.speaking.on('start', (userId) => {
            if (activeRecordings.has(userId)) {
                const existing = activeRecordings.get(userId);
                if (existing.stopTimeout) {
                    clearTimeout(existing.stopTimeout);
                    existing.stopTimeout = null;
                }
                return;
            }

            const user = client.users.cache.get(userId);
            const userName = user ? user.username : userId;
            const filename = path.join(recordingsDir, `audio_${userName}_${Date.now()}.ogg`);
            
            console.log(`[START] 🎙️ Nagrywanie dla: ${userName}`);

            const fileStream = fs.createWriteStream(filename);
            
            // --- KLUCZOWA ZMIANA: Słuchamy 'finish' już TERAZ ---
            fileStream.on('finish', () => {
                console.log(`[PLIK] 💾 Zapis na dysku zakończony: ${path.basename(filename)}`);
                uploadToAzure(filename);
            });

            const audioStream = receiver.subscribe(userId, {
                end: { behavior: EndBehaviorType.Manual }
            });

            const ffmpegProcess = new prism.FFmpeg({
                args: [
                    '-f', 'opus', '-i', 'pipe:0',
                    '-c:a', 'copy', '-f', 'ogg', 'pipe:1',
                ],
            });

            audioStream.pipe(ffmpegProcess).pipe(fileStream);

            activeRecordings.set(userId, {
                audioStream,
                ffmpegProcess,
                fileStream,
                userName,
                filename,
                stopTimeout: null
            });
        });

        receiver.speaking.on('end', (userId) => {
            const rec = activeRecordings.get(userId);
            if (!rec) return;

            rec.stopTimeout = setTimeout(() => {
                console.log(`[CISZA] 🛑 3s u ${rec.userName} -> Zamykam.`);
                rec.audioStream.destroy();
                rec.ffmpegProcess.destroy();
                rec.fileStream.end();
                activeRecordings.delete(userId);
            }, 3000);
        });
    }

    if (message.content === '!stop') {
        console.log(`[KOMENDA] Wykryto !stop. Czyszczenie...`);
        if (currentConnection) {
            activeRecordings.forEach((rec, userId) => {
                console.log(`[STOP] Wymuszam koniec dla ${rec.userName}`);
                if (rec.stopTimeout) clearTimeout(rec.stopTimeout);
                rec.audioStream.destroy();
                rec.ffmpegProcess.destroy();
                rec.fileStream.end();
            });
            activeRecordings.clear();
            currentConnection.destroy();
            currentConnection = null;
            message.reply('⏹️ Rozłączono i wysłano pliki.');
        }
    }
});

client.login(process.env.DISCORD_TOKEN);
