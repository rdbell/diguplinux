# Third-party notices

The native embedding pipeline in `src/diguplinux/native/worker.cpp` is adapted
from `Sources/LlamaRuntime/LlamaEmbedder.swift` in
[DigUp](https://github.com/ARahim3/DigUp). The `no-logits.patch` memory fix and
pinned model checksums also come from DigUp. DigUp is licensed as follows:

MIT License

Copyright (c) 2026 Abdur Rahim

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

`digup runtime` fetches [llama.cpp](https://github.com/ggml-org/llama.cpp) at
commit `4d756bc72bf00a4aacf410ae15a2d315f3db400d` (tag `b11461`). Its MIT license
and its bundled dependency licenses remain in the downloaded source tree. The
build installs a copy of its main license alongside the worker. Its vendor
dependencies include nlohmann/json, miniaudio, stb and other components; consult
the pinned source's `vendor/` directory before redistributing a compiled worker.

The model weights are downloaded separately from
[ggml-org/embeddinggemma-2-GGUF](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF).
[EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) is Apache-2.0
licensed. This repository does not bundle model weights, llama.cpp source, or
compiled workers. System extraction tools are invoked as separate programs.


## keystroke-hister

The Keystroke extension's provider lifecycle and installer are adapted from
[rdbell/keystroke-hister](https://github.com/rdbell/keystroke-hister), copyright
2026 rdbell, under the MIT license. The full notice is preserved in
`extensions/keystroke-digup/LICENSE`.

The demo renderer and desktop wallpaper are also adapted from the same
MIT-licensed keystroke-hister project. The full copyright and permission
notice is included in `extensions/keystroke-digup/LICENSE`.
