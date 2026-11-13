# Clipper Plus upstream source and attribution

Source: https://github.com/obsidianmd/obsidian-clipper

Vendored commit: `6d56d618b00bd970aa738d6a7a61edee27783e81` (upstream version 1.7.1).
The upstream MIT license and copyright notices are preserved. Upstream templates,
properties, extraction, Reader, Highlighter, Interpreter, settings, and save flows
remain part of this independent project.

Clipper Plus adds local video subtitle extraction and transcription. These additions
are implemented under `src/videomind/` with a Chrome Native Messaging bridge to the
Windows background engine. Original video Markdown is rendered in the extension and
can be saved to the configured local directory or sent through the existing clipper
workflow. There is no cloud AI summary feature.

The Chrome build uses `src/manifest.chrome.json`; the extension name, action title,
and independent Clipper Plus icon are configured there. Existing Windows host IDs,
task and pipe names, data directory, and extension ID remain stable to support
upgrades from VideoMind installations.

Build: `npm ci && npm run build:chrome`. Load `dist/` unpacked in Chrome and use the
extension ID during Setup. The Native Messaging host identifier remains
`com.videomind.host` for compatibility.
