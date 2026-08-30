# Third-party notices

This distribution combines unmodified third-party runtimes and command-line tools.
Their upstream license terms continue to apply.

- Python 3.11: Python Software Foundation License — https://www.python.org/psf/license/
- FFmpeg: this bundled build reports GPL v3 configuration — https://ffmpeg.org/legal.html
- yt-dlp: The Unlicense — https://github.com/yt-dlp/yt-dlp
- Deno: MIT License — https://github.com/denoland/deno
- faster-whisper and CTranslate2: MIT License — https://github.com/SYSTRAN/faster-whisper and https://github.com/OpenNMT/CTranslate2
- OpenAI Python SDK: Apache-2.0 — https://github.com/openai/openai-python
- biliup: see the license shipped under `biliup/bbup-app/binaries/_internal/biliup-1.2.2.dist-info/licenses/`.
- NVIDIA CUDA runtime and cuDNN files in the GPU edition: NVIDIA license terms apply.
- Whisper/CTranslate2 model files: retain and review the model repository's accompanying README and metadata before public redistribution.

`portable_manifest.json` records the selected edition and component versions. Python
package metadata and license files are retained under `runtime/python/Lib/site-packages`.

Before a public or commercial release, the distributor should verify that the exact
FFmpeg binary source and corresponding license/source-offer obligations are satisfied.
