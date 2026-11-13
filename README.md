<p align="right">
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Language-简体中文-7048E8?style=for-the-badge" alt="阅读简体中文版"></a>
</p>

<p align="center">
  <img src="assets/clipper-plus-cover.png" width="440" alt="Clipper Plus: a document, paperclip, and video symbol on a purple background">
</p>

<h1 align="center">Clipper Plus</h1>
<p align="center"><strong>Clip web pages and turn video speech into editable Markdown.</strong></p>
<p align="center">
  <img src="https://img.shields.io/badge/Chrome-Manifest%20V3-7048E8?style=flat-square" alt="Chrome Manifest V3">
  <img src="https://img.shields.io/badge/Local%20engine-Windows%2010%2F11-7048E8?style=flat-square" alt="Windows 10 and 11 local engine">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-7048E8?style=flat-square" alt="MIT License"></a>
</p>
<p align="center">
  <a href="#features">Features</a> · <a href="#get-started">Get started</a> · <a href="#how-it-works">How it works</a> · <a href="#development">Development</a>
</p>

---

## What is Clipper Plus?

Clipper Plus extends [Obsidian Web Clipper](https://github.com/obsidianmd/obsidian-clipper) with a local video transcription workflow. The familiar clipper handles ordinary web pages, templates, Reader, highlights, and saving to Obsidian or Markdown. On a video page, it can use subtitle text extracted from the page or ask a separate Windows engine to transcribe the audio with faster-whisper.

The Chrome extension and Windows engine are both needed for local transcription. Ordinary web clipping can still be used when the engine is unavailable.

## Features

| Feature | What it does |
| --- | --- |
| Web clipping | Extract page content and metadata, apply templates, then save to Obsidian, copy, or download Markdown. |
| Video subtitles | Use subtitle text already extracted by the clipper when available; choose whether to force audio transcription. |
| Local transcription | Download audio with yt-dlp, convert it with FFmpeg, and transcribe it locally with faster-whisper. |
| Editable Markdown | Produce a note with video metadata and timestamped transcript lines, then edit it in the clipper. |
| History | Reopen completed transcriptions from the local cache, search records, and manage them in Settings. |
| Reading tools | Keep the upstream Reader, highlighter, templates, and optional Interpreter features. |

> [!NOTE]
> This version does **not** generate an AI video summary. The upstream Interpreter is a separate, optional feature that may contact a model provider if you configure and use it.

## Get started

### Requirements

- Windows 10 or 11 and Google Chrome.
- Python 3.11–3.13 with the `py` launcher.
- FFmpeg and FFprobe available on `PATH`.
- At least 3 GiB of free disk space for Setup.
- Node.js and npm to build the extension from source.
- Deno 2.3.0 or later for YouTube's JavaScript challenges. Setup can offer a per-user installation through `winget` if Deno is missing.

### Install from source

1. Clone the repository and build the Chrome extension:

   ```powershell
   git clone https://github.com/callmepingping/Clipper-Plus.git
   cd Clipper-Plus\extension
   npm ci
   npm run build:chrome
   ```

2. In `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked**, and select `extension/dist`. Copy the extension's 32-character ID shown by Chrome.
3. Run `setup/run-setup.bat` from the repository. Setup checks prerequisites, asks for the Chrome extension ID, creates the local Python environment, registers the Native Messaging host, and starts the background engine. Follow its prompts; it may offer to install Deno.
4. Open a video page and click the Clipper Plus toolbar icon. If the page already provides subtitle text, use the clipper result or confirm forced audio transcription. Otherwise, transcription starts automatically.

Setup is interactive and currently uses English prompts. Run `py -3 setup/main.py --check` from the repository root for read-only installation diagnostics. If Chrome assigns a different extension ID after reloading from another folder, run Setup again with the new ID.

## How it works

```text
Web page ──> Chrome extension ──> extracted content / subtitles
                         │
                         └──> Native Messaging host
                                      │
                                      └──> Windows background engine
                                             ├── yt-dlp + FFmpeg: audio
                                             ├── faster-whisper: speech
                                             └── local Markdown cache + history
```

The Native Messaging host passes short requests to an independent engine over a per-user Windows named pipe. Closing the popup or Chrome does not stop an already running engine task. Task progress is transient; completed Markdown and history are stored under the local `runtime/` directory. Setup creates that directory, and Git ignores it.

### Data and current limits

- Audio transcription runs on the user's machine. yt-dlp still contacts the video source to obtain audio; model files may be downloaded on first use.
- Video content is not sent to a cloud summarization service by this workflow. The optional upstream Interpreter has its own provider settings.
- The engine processes one transcription task at a time. It does not resume an interrupted task after the engine or Windows exits.
- Video page detection and yt-dlp support both affect which sites work. Private, restricted, or DRM-protected content is not guaranteed to work.
- The local transcription engine is supported on Windows. Browser builds for other platforms do not provide this Windows engine.

## Development

| Path | Purpose |
| --- | --- |
| `extension/src/` | Browser extension UI and upstream clipper features |
| `extension/src/videomind/` | Video popup, native bridge, protocol, history, and rendering |
| `setup/` | Interactive installation and Windows host registration |
| `setup/engine/videomind/` | Background engine, transcription, IPC, storage, and cache |
| `runtime/` | Local user data created by Setup; excluded from Git |

```powershell
cd extension
npm ci
npm run typecheck
npm run test:videomind
npm run build:chrome
```

The Python tests use the standard library's `unittest` runner after the Setup environment is available:

```powershell
cd setup
.\.build\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q
```

Contributions and bug reports are welcome through [GitHub Issues](https://github.com/callmepingping/Clipper-Plus/issues). Please include the operating system, browser version, steps to reproduce, and any safe error code shown by the extension. Do not attach private video content, cookies, or local configuration files.

## Attribution and license

Clipper Plus is based on [Obsidian Web Clipper](https://github.com/obsidianmd/obsidian-clipper). See the [upstream source and attribution note](extension/VIDEOMIND_UPSTREAM.md) and the preserved [upstream MIT license](extension/LICENSE). Clipper Plus's own code is released under the [MIT License](LICENSE).
