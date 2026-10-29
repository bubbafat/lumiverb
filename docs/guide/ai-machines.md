# AI machines

Some of Lumiverb's work is done by an AI model on a machine you choose: a computer running Ollama, or a cloud service. This page covers adding machines, choosing models, and what it costs.

## What needs a machine

Lumiverb's AI work is four jobs, each with one model for the whole account:

| Job | What it makes | Out of the box |
|---|---|---|
| Descriptions & text | Photos' descriptions and tags, text in photos (signs, screens, documents), descriptions of video scenes, and each video's description | Off: needs a machine and a model |
| Transcripts | What's said in videos | The Built in machine, model `small` |
| Visual search (CLIP) | Similar photos, search by picture, and the same vectors of video scenes' pictures | The Built in machine |
| Faces | Faces in photos and videos | The Built in machine |

Until Descriptions & text has a machine and a model, that work waits. Nothing fails, and once you add one it catches up on everything.

Everyone can see Settings › AI: each machine's name, its jobs and how it's doing. Only an admin sees each machine's address, which can name a computer on your network, and only an admin changes it.

## The Built in machine

Every account has a machine called **Built in**: the server's own computer, doing transcripts, visual search and faces with nothing to set up. **Edit** renames it, changes its **Requests at once**, unticks jobs, or turns it off (**Use this machine**). It can't be removed.

On a Mac, its transcripts run on the processor, not the GPU. To transcribe faster, run an OpenAI-compatible Whisper server (such as [speaches](https://speaches.ai)) on a computer with an NVIDIA GPU, download the job's model into it, and add it as a machine doing **Transcripts**. For visual search and faces on another computer, see [A second machine for search and faces](ai-worker.md).

## Adding a machine

1. Open **Settings › AI** and click **Add machine**.
2. Under **Where**, choose Ollama on this computer, Ollama on another computer, OpenAI, Anthropic (Claude), Google Gemini, or Custom URL (any other OpenAI-compatible server). It fills in the **Endpoint URL** and **Name**, and says whether it needs a key.
3. Paste the **API key**, if it needs one.
4. For an `https://` URL whose certificate no public authority signed, such as Lumiverb's own [AI worker](ai-worker.md), paste its **Certificate fingerprint**: Lumiverb then takes that certificate from it and no other. Leave it empty for a cloud service: its certificate is checked as any https site's is.
5. Click **Connect**. It lists the models the machine offers.
6. Under **Does**, tick its jobs. Those whose model it offers are ticked already.
7. Set **Requests at once**: how many it works on together. More needs more memory there.
8. If it shares a GPU with Lumiverb on the same computer, tick **Shares the GPU with video work here**: it gets fewer requests while videos are decoded.
9. Click **Add**.

Then choose the job's model under **Models** (below). Work is spread across every machine online and doing the job.

## Ollama

[Ollama](https://ollama.com) runs models on your own computer, so your photos stay on your network. The model to use is `qwen3-vl:8b-instruct`: a 6 GB download, which used about 10 GB of memory on a Mac Studio. Take the `-instruct` one: plain `qwen3-vl:8b` thinks before it answers, and most descriptions come back empty. `qwen3-vl:4b-instruct` is smaller, but hasn't been tested.

On a Mac:

```bash
brew install ollama
```

```bash
brew services start ollama
```

```bash
ollama pull qwen3-vl:8b-instruct
```

On Linux, install the NVIDIA driver first (`nvidia-smi` should work): without it, Ollama runs on the processor and a photo takes minutes. Then:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

```bash
ollama pull qwen3-vl:8b-instruct
```

In Settings › AI, add it with **Ollama on this computer** (`http://localhost:11434/v1`), tick **Descriptions & text**, then choose `qwen3-vl:8b-instruct` under Models.

For Ollama on another computer, choose **Ollama on another computer** and put that computer's address in the URL. Ollama there must listen on the network, and has no password: do this only on a network you trust. On Linux, add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]` in:

```bash
sudo systemctl edit ollama.service
```

```bash
sudo systemctl restart ollama
```

On a Mac, see [Ollama's FAQ](https://docs.ollama.com/faq).

Leave **Requests at once** at 1 unless that Ollama is set to run several requests in parallel (`OLLAMA_NUM_PARALLEL`).

## Cloud services

OpenAI, Anthropic and Google Gemini each need an API key from their own site. Settings › AI says where:

| Where | Key from | Works for descriptions & text |
|---|---|---|
| OpenAI | platform.openai.com › API keys | `gpt-4.1-mini` |
| Anthropic (Claude) | console.anthropic.com › API keys | `claude-haiku-4-5-20251001` |
| Google Gemini | aistudio.google.com › API keys | One that doesn't think before it answers (a lite one). Its models are listed as `models/…` |

A cloud machine is sent a preview of every photo, and frames of every video's scenes, to describe them. They leave your network.

## Choosing each job's model

Under **Models**, each job shows its model (or off) and how many of its machines offer it. Click **Change**, pick a model, and click **Save**. Only models a machine doing that job offers are listed. Visual search and Faces have a fixed model.

Changing a model that has already made things asks what becomes of them:

- **New files only**: what the old model made stays as it is; the new model is for what's new. You can remake the rest later from Settings › Processing.
- **Also remake N clips**: everything is made again with the new model, after all other work. Until that's done, results mix the two. Settings › Processing shows how far it has got, and can stop it.

**Turn off descriptions & text** (or another job) stops that job, and remakes nothing.

## Testing a machine

Click a machine's **Test** before it gets real work. It's sent what descriptions & text sends, with the job's model: a picture Lumiverb draws (none of your photos) and a video's scenes. Each request shows **Answered as the work needs it** or what went wrong, how long it took and the room its answer used; **Its answer** shows what came back. A model that thinks first, or refuses Lumiverb's settings, shows up here.

## When a machine is offline

Lumiverb checks every machine each minute. Each shows **Online**, or **Offline** with the reason; an offline one's work goes to the others. With no machine for a job online, Settings › AI shows the job as paused, with a dot beside AI in Settings. The work waits until it's fixed, and no clip is marked as failed for it.

The reason is in Lumiverb's words, such as "refused the key" or "answered 500", as everyone signed in can see it. What the machine itself answered is in the server's log, **Admin › Logs**, for the server's admin.

Lumiverb sends a machine's work, and its key, only to the address you gave it. A machine that answers with a redirect to somewhere else is offline: Lumiverb doesn't follow it. The address is checked again at every request, as when the machine was added: this computer and your network are fine; link-local and cloud metadata addresses never are.

A machine added with a certificate fingerprint whose certificate isn't that one any more is offline too, saying "Its certificate isn't the one it was added with": nothing is sent to it. If its certificate really changed, **Edit** it and give the new fingerprint.

## Unencrypted machines

A machine at an `http://` address on another computer is marked **Unencrypted**: its key and the photos it's sent cross your network as they are. Ollama serves only plain HTTP, so use one on another computer only on a network you trust. Lumiverb's own AI worker serves HTTPS: one set up before it did is marked with how to switch it (see [A second machine for search and faces](ai-worker.md#a-worker-from-before-https)). A machine on this computer (`localhost`) crosses no network and isn't marked.

## Keys stay sealed

A machine's key is never shown again once saved, to anyone. Editing a machine, leave the key blank to keep it, or tick **Remove the saved key**. Keys are sealed in the database with a key from the install's env file, so a backup alone can't read them: keep a copy of the env file with your backups (see [Backups and restore](backups.md#keep-a-copy-of-the-env-file)). Restored without it, a machine says its saved key can't be read here: enter it again.

## Costs

A cloud service charges for each request. Each photo is two requests (its description, and its text). Each video is one request per scene, and one for its description. In a big library that adds up: try **Test** first, and watch Settings › Processing, where work can be paused (see [Processing](processing.md)).

An update to Lumiverb never makes your machines redo work on its own. When a new version would make something differently, what was made before is held until an admin chooses **Remake** for it in Settings › Processing.

## From the command line

On a server with no screen, with an admin's key:

```bash
lumiverb ai list
```

```bash
lumiverb ai machines add Ollama --url http://localhost:11434/v1 --jobs vision
```

```bash
lumiverb ai model vision qwen3-vl:8b-instruct
```

```bash
lumiverb ai machines test Ollama
```

The jobs are called `vision` (descriptions & text), `transcripts`, `clip` and `faces`. `--key` asks for a machine's key at a prompt, so it never appears on the command line. `--fingerprint` gives an https machine's certificate fingerprint (on `add`, `edit` and `models`; `--fingerprint ''` on `edit` forgets it). More in [The command line](command-line.md).
