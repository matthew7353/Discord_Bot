require('dotenv').config();
const { Client, GatewayIntentBits } = require('discord.js');
const { joinVoiceChannel, EndBehaviorType, VoiceConnectionStatus, generateDependencyReport } = require('@discordjs/voice');
const fs = require('fs');
const path = require('path');

// --- BEZPOŚREDNI IMPORT KLAS OGG (Obejście błędu prism-media) ---
const prism = require('prism-media');
// ----------------------------------------------------------------

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

// Pobieranie tokena z pliku .env
const TOKEN = process.env.DISCORD_TOKEN;

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

        console.log("Czyszczenie starych plików przed nową sesją...");
        const oldFiles = fs.readdirSync(recordingsDir);
        for (const file of oldFiles) {
            if (file.endsWith('.ogg')) fs.unlinkSync(path.join(recordingsDir, file));
        }

        currentConnection = joinVoiceChannel({
            channelId: voiceChannel.id,
            guildId: message.guild.id,
            adapterCreator: message.guild.voiceAdapterCreator,
            selfDeaf: false,
            selfMute: false
        });

        currentConnection.on('stateChange', (oldState, newState) => {
            console.log(`[VOICE] Zmiana stanu: ${oldState.status} -> ${newState.status}`);
        });

        currentConnection.on('error', (error) => {
            console.error(`[VOICE KRYTYCZNY BŁĄD]:`, error);
        });

        currentConnection.on(VoiceConnectionStatus.Ready, () => {
            console.log(`Połączono z kanałem ${voiceChannel.name}! Czekam na dźwięk...`);
            
            // Dodane catch na wypadek braku uprawnień na kanale tekstowym!
            message.reply(`Wszedłem na kanał! Zaczynam nasłuch.`)
                .catch(err => console.error("[BŁĄD DISCORDA] Nie mam uprawnień do pisania na tym kanale!", err.message));

            const receiver = currentConnection.receiver;

            receiver.speaking.on('start', (userId) => {
                if (activeRecordings.has(userId)) return;

                console.log(`[AUDIO] Użytkownik ${userId} zaczął mówić... Tworzę plik Ogg/Opus.`);
                activeRecordings.set(userId, true);

                const user = client.users.cache.get(userId);
                const userName = user ? user.username : userId;
                
                const filename = path.join(recordingsDir, `audio_${userName}_${Date.now()}.ogg`);
                const fileStream = fs.createWriteStream(filename);

                const audioStream = receiver.subscribe(userId, {
                    end: {
                        behavior: EndBehaviorType.AfterSilence,
                        duration: 3000 
                    },
                });

                // Zgodnie ze wskazówką z obrazka: używamy strumieniowania prosto do formatu Ogg bez dekodowania do PCM.
                // Wykorzystujemy nasze naprawione, bezpośrednio zaimportowane klasy:
		// -i pipe:0: czytaj ze standardowego wejścia
		// -c:a copy: nie re-koduj, po prostu kopiuj strumień (0% straty jakości, minimalne CPU)
		// -f ogg: format wyjściowy OGG
		const ffmpegProcess = new prism.FFmpeg({
		    args: [
		        '-f', 'opus',
		        '-i', 'pipe:0',
		        '-c:a', 'copy',
		        '-f', 'ogg',
		        'pipe:1',
		    ],
		});

		audioStream.on('error', (err) => {
		    console.warn(`[AUDIO-OSTRZEŻENIE] Błąd strumienia: ${err.message}`);
		});

		// Łączymy: Discord -> FFmpeg (Pakowanie) -> Plik
		audioStream.pipe(ffmpegProcess).pipe(fileStream);
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
            
            message.reply('Rozłączono z kanału. Zapisywanie nagrań zakończone. 🛑').catch(console.error);
            console.log('Bot opuścił kanał. Zapisywanie audio zostało zakończone.');

        } else {
            message.channel.send('Bot nie jest połączony z żadnym kanałem.').catch(console.error);
        }
    }
});

client.login(TOKEN);
