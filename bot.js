const { Client, GatewayIntentBits } = require('discord.js');
const { joinVoiceChannel, EndBehaviorType, VoiceConnectionStatus, generateDependencyReport } = require('@discordjs/voice');
const prism = require('prism-media');
const fs = require('fs');
const path = require('path');

console.log("=== RAPORT ZALEŻNOŚCI GŁOSOWYCH ===");
console.log(generateDependencyReport());
console.log("===================================");

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildVoiceStates,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
    ]
});

// TUTAJ WPISZ NOWY TOKEN PO JEGO ZREGENEROWANIU NA DISCORD DEVELOPER PORTAL
const TOKEN = 'TWÓJ_NOWY_BEZPIECZNY_TOKEN'; 

const recordingsDir = path.join(__dirname, 'recordings');
if (!fs.existsSync(recordingsDir)) {
    fs.mkdirSync(recordingsDir);
}

let currentConnection = null;
const activeRecordings = new Map();

client.on('clientReady', () => {
    console.log(`Zalogowano jako: ${client.user.tag}`);
    console.log(`Gotowy do działania.`);
});

client.on('messageCreate', async (message) => {
    if (message.author.bot) return;

    if (message.content === '!start') {
        const voiceChannel = message.member?.voice.channel;
        
        if (!voiceChannel) {
            return message.reply('Musisz najpierw wejść na kanał głosowy!');
        }

        // --- CZYSZCZENIE STARYCH DANYCH ---
        console.log("Czyszczenie starych plików przed nową sesją...");
 
        const oldFiles = fs.readdirSync(recordingsDir);
        for (const file of oldFiles) {
            fs.unlinkSync(path.join(recordingsDir, file));
        }
        if (fs.existsSync('transcript.txt')) fs.unlinkSync('transcript.txt');
        if (fs.existsSync('notatka.md')) fs.unlinkSync('notatka.md');
        // ----------------------------------

        currentConnection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: message.guild.id,
            adapterCreator: message.guild.voiceAdapterCreator,
            selfDeaf: false,
            selfMute: false
        });

        currentConnection.on(VoiceConnectionStatus.Ready, () => {
            console.log(`Połączono z kanałem ${voiceChannel.name}! Czekam na dźwięk...`);
            message.reply(`Wszedłem na kanał! Zaczynam nasłuch.`);

            const receiver = currentConnection.receiver;

            receiver.speaking.on('start', (userId) => {
                if (activeRecordings.has(userId)) return;

                console.log(`[AUDIO] Użytkownik ${userId} zaczął mówić... Tworzę plik Ogg/Opus.`);
                activeRecordings.set(userId, true);

                const user = client.users.cache.get(userId);
                const userName = user ? user.username : userId;
                
                // Zmiana rozszerzenia z pcm na ogg
                const filename = path.join(recordingsDir, `audio_${userName}_${Date.now()}.ogg`);
                const fileStream = fs.createWriteStream(filename);

                const audioStream = receiver.subscribe(userId, {
                    end: {
                        behavior: EndBehaviorType.AfterSilence,
                        duration: 3000 
                    },
                });

                // Zamiast dekodera PCM, używamy OggLogicalBitstream, 
                // aby zapisać surowe pakiety Opus prosto do kontenera Ogg.
                const oggStream = new prism.opus.OggLogicalBitstream({
                    opusHead: new prism.opus.OpusHead({
                        channelCount: 2,
                        sampleRate: 48000,
                    }),
                    pageSizeControl: {
                        maxPackets: 10,
                    },
                });

                audioStream.on('error', (err) => {
                    console.warn(`[AUDIO-OSTRZEŻENIE] Błąd strumienia: ${err.message}`);
                });

                // Pipowanie surowego audio -> kontener Ogg -> plik na dysku
                audioStream.pipe(oggStream).pipe(fileStream);

                audioStream.on('end', () => {
                    console.log(`[AUDIO] 🟩 Cisza! Zamknięto plik: ${filename}`);
                    activeRecordings.delete(userId);
                });
            });
        });
    }

    if (message.content === '!stop') {
        if (currentConnection) {
            currentConnection.destroy();
            currentConnection = null;
            
            message.reply('Rozłączono z kanału. Zapisywanie nagrań zakończone. 🛑');
            console.log('Bot opuścił kanał. Zapisywanie audio zostało zakończone (skrypty Pythona wyłączone).');

        } else {
            message.channel.send('Bot nie jest połączony z żadnym kanałem.');
        }
    }
});

client.login(TOKEN);
