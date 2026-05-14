# Discord Bot

## Installation

### Node.js

```
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install nodejs -y
```

You can check if it was succesfully installed by using:

```
node -v
npm -v
```
### ffmpeg

```
sudo apt install ffmpeg -y
```

### Node packages installation

```
npm install
```

### dotenv

```
npm install dotenv
```

### pm2

```
sudo npm install -g pm2
```

## Running

You need to add Discord Token to dotenv.

```
DISCORD_TOKEN=twoj_token
```

Then run the script in pm2 by using:

```
pm2 start bot.js --name discord-bot
```

### Useful commands for pm2

```
pm2 status
pm2 logs discord-bot
pm2 stop discord-bot
```
