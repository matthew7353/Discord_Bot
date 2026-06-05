require('dotenv').config();
const { Client, GatewayIntentBits } = require('discord.js');
const { joinVoiceChannel, EndBehaviorType } = require('@discordjs/voice');
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
                if (existing.stopTimeout) clearTimeout(existing.stopTimeout);
                return;
            }

            const user = client.users.cache.get(userId);
            const userName = user ? user.username : userId;
            
            // Format .pcm
            const filename = path.join(recordingsDir, `audio_${userName}_${Date.now()}.pcm`);
            
            console.log(`[START] 🎙️ Nagrywanie dla: ${userName}`);

            const fileStream = fs.createWriteStream(filename);
            
            // To zdarzenie wywoła wysyłkę do Azure, gdy zawołamy fileStream.end()
            fileStream.on('finish', () => uploadToAzure(filename));

            const audioStream = receiver.subscribe(userId, { end: { behavior: EndBehaviorType.Manual } });

            const opusDecoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });

            audioStream.on('error', (err) => console.error(`[Audio BŁĄD]: ${err.message}`));
            opusDecoder.on('error', (err) => console.error(`[Decoder BŁĄD]: ${err.message}`));

            audioStream.pipe(opusDecoder).pipe(fileStream);

            // ZMIANA: Zapisujemy fileStream w pamięci RAM, żeby móc go potem wyłączyć
            activeRecordings.set(userId, { audioStream, fileStream, userName, stopTimeout: null });
        });

        receiver.speaking.on('end', (userId) => {
            const rec = activeRecordings.get(userId);
            if (!rec) return;

            rec.stopTimeout = setTimeout(() => {
                console.log(`[CISZA] 🛑 3s u ${rec.userName} -> Zamykam.`);
                
                if (rec.audioStream && !rec.audioStream.destroyed) rec.audioStream.destroy();
                // ZMIANA: Wymuszamy zamknięcie pliku, co natychmiast odpala wysyłkę do Azure!
                if (rec.fileStream) rec.fileStream.end(); 
                
                activeRecordings.delete(userId);
            }, 3000);
        });
    }

    if (message.content === '!stop') {
        if (currentConnection) {
            activeRecordings.forEach((rec) => {
                if (rec.stopTimeout) clearTimeout(rec.stopTimeout);
                if (rec.audioStream && !rec.audioStream.destroyed) rec.audioStream.destroy();
                // ZMIANA: Zamykamy plik również przy wymuszonym stopie
                if (rec.fileStream) rec.fileStream.end();
            });
            activeRecordings.clear();
            currentConnection.destroy();
            currentConnection = null;
            message.reply('⏹️ Rozłączono i rozpoczęto wysyłkę plików.');
        }
    }
});

client.login(process.env.DISCORD_TOKEN);
