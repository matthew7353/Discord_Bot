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

## Configuration

Copy `.env.example` to `.env` and fill it in:

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `AZURE_STORAGE_CONNECTION_STRING` | Storage Account shared with the processing job |
| `AZURE_STORAGE_CONTAINER` | Blob container (default `audio-files`, must match the job) |
| `AZURE_SUBSCRIPTION_ID` | Subscription of the Container Apps Job |
| `AZURE_RESOURCE_GROUP` | Resource group of the Container Apps Job |
| `AZURE_PROCESSING_JOB_NAME` | Name of the Container Apps Job (branch `azure-job`), e.g. `dnd-notes-job` |

## Processing (Azure Container Apps Job)

Transcription and notes are produced by the Azure Container Apps Job built from
the `azure-job` branch. After `!stop` the bot:

1. uploads `sessions/<id>/audio_<user>_<timestamp>.pcm` files to blob storage,
2. writes `sessions/<id>/status.json` with status `recorded`,
3. starts the job (unless an execution is already running — it will pick the session up),
4. polls `status.json` (`recorded` → `processing` → `transcribed` → `done` / `error`)
   and posts `notes.md` to the channel when it is `done`. If the job ends while the
   session is still pending, the bot restarts it (at most twice).

The VM running the bot needs a System-Assigned Managed Identity with a role on the
job that allows `Microsoft.App/jobs/start/action` and
`Microsoft.App/jobs/executions/read` (e.g. `Contributor` scoped to the job):

```
az vm identity assign -g <bot-rg> -n <bot-vm>
az role assignment create --assignee <principal-id> --role Contributor \
  --scope $(az containerapp job show -g <rg> -n dnd-notes-job --query id -o tsv)
```

## Running

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
