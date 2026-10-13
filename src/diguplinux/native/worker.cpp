// Embedding pipeline adapted from DigUp's LlamaEmbedder.swift (MIT).
// See THIRD_PARTY_NOTICES.md for attribution and license.
#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"
#include "nlohmann/json.hpp"
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;

static void log_callback(ggml_log_level level, const char * text, void *) {
    if (level == GGML_LOG_LEVEL_ERROR || level == GGML_LOG_LEVEL_WARN) std::cerr << text;
}

class Encoder {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    mtmd_context * media = nullptr;
    llama_batch_ext * batch = nullptr;
    int max_tokens = 2048;
    int position = 0;
    std::vector<ggml_backend_dev_t> devices;

    void place(int index) {
        llama_pos pos[] = {position++, 0, 0, 0};
        if (index < 0 || position > max_tokens ||
            !llama_batch_ext_set_pos(batch, index, pos) ||
            !llama_batch_ext_set_output_embd(batch, index, true)) {
            throw std::runtime_error("input exceeds embedding batch capacity");
        }
    }
    void add(llama_token token) { place(llama_batch_ext_add_token(batch, 0, token)); }
    void reset() { llama_batch_ext_clear(batch); position = 0; }
    std::vector<float> run() {
        if (llama_process(ctx, LLAMA_PROCESS_TYPE_ENCODE, batch) != 0)
            throw std::runtime_error("llama_process failed");
        auto pooled = llama_get_embeddings_seq(ctx, 0);
        if (!pooled) throw std::runtime_error("model returned no pooled embedding");
        int dim = llama_model_n_embd_out(model);
        double norm = 0;
        for (int i = 0; i < dim; ++i) norm += double(pooled[i]) * pooled[i];
        if (!std::isfinite(norm) || norm <= 0) throw std::runtime_error("invalid embedding norm");
        std::vector<float> result(dim);
        for (int i = 0; i < dim; ++i) result[i] = pooled[i] / std::sqrt(norm);
        return result;
    }
    void release() {
        if (batch) llama_batch_ext_free(batch);
        if (media) mtmd_free(media);
        if (ctx) llama_free(ctx);
        if (model) llama_model_free(model);
        batch = nullptr; media = nullptr; ctx = nullptr; model = nullptr;
    }
public:
    Encoder(const std::string & text_path, const std::string & media_path, int threads, int gpu_layers) {
        try {
            auto mp = llama_model_default_params();
            mp.n_gpu_layers = gpu_layers;
            if (gpu_layers != 0) {
                if (!llama_supports_gpu_offload()) throw std::runtime_error("worker was built without GPU support");
                for (size_t i = 0; i < ggml_backend_dev_count(); ++i) {
                    auto device = ggml_backend_dev_get(i);
                    auto type = ggml_backend_dev_type(device);
                    std::string description = ggml_backend_dev_description(device);
                    std::transform(description.begin(), description.end(), description.begin(),
                                   [](unsigned char c) { return std::tolower(c); });
                    if ((type == GGML_BACKEND_DEVICE_TYPE_GPU || type == GGML_BACKEND_DEVICE_TYPE_IGPU)
                        && description.find("llvmpipe") == std::string::npos
                        && description.find("lavapipe") == std::string::npos) devices.push_back(device);
                }
                if (devices.empty()) throw std::runtime_error("no hardware GPU available; refusing CPU fallback");
            }
            devices.push_back(nullptr);
            mp.devices = devices.data();
            model = llama_model_load_from_file(text_path.c_str(), mp);
            if (!model) throw std::runtime_error("cannot load text model");
            auto cp = llama_context_default_params();
            max_tokens = 2048;
            cp.n_ctx = cp.n_batch = cp.n_ubatch = max_tokens;
            cp.n_seq_max = 1;
            cp.n_threads = cp.n_threads_batch = threads;
            cp.embeddings = true;
            cp.offload_kqv = gpu_layers != 0;
            cp.op_offload = gpu_layers != 0;
            cp.pooling_type = LLAMA_POOLING_TYPE_MEAN;
            ctx = llama_init_from_model(model, cp);
            if (!ctx) throw std::runtime_error("cannot create embedding context");
            batch = llama_batch_ext_init(ctx);
            if (!batch) throw std::runtime_error("cannot allocate embedding batch");
            if (!media_path.empty()) {
                auto mm = mtmd_context_params_default();
                mm.use_gpu = gpu_layers != 0;
                mm.n_threads = threads;
                mm.print_timings = false;
                mm.image_min_tokens = 70;
                mm.image_max_tokens = 280;
                media = mtmd_init_from_file(media_path.c_str(), model, mm);
                if (!media) throw std::runtime_error("cannot load media encoders");
            }
        } catch (...) { release(); throw; }
    }
    ~Encoder() { release(); }
    json info() const {
        json names = json::array();
        for (auto device : devices) if (device) names.push_back(ggml_backend_dev_description(device));
        return {{"ready", true}, {"dimension", llama_model_n_embd_out(model)},
                {"backend", DIGUP_BACKEND}, {"gpu", devices.size() > 1}, {"devices", names}};
    }
    std::vector<float> text(const std::string & value) {
        reset();
        auto vocab = llama_model_get_vocab(model);
        std::vector<llama_token> tokens(value.size() + 8);
        int count = llama_tokenize(vocab, value.c_str(), value.size(), tokens.data(), tokens.size(), true, true);
        if (count < 0) {
            tokens.resize(-count);
            count = llama_tokenize(vocab, value.c_str(), value.size(), tokens.data(), tokens.size(), true, true);
        }
        if (count <= 0) throw std::runtime_error("cannot tokenize text");
        tokens.resize(count);
        if (count > max_tokens) { tokens.resize(max_tokens); tokens.back() = llama_vocab_eos(vocab); }
        for (auto token : tokens) add(token);
        return run();
    }
    std::vector<float> file(const std::string & path, bool image) {
        if (!media) throw std::runtime_error("worker started without media encoders");
        auto loaded = mtmd_helper_bitmap_init_from_file(media, path.c_str(), false, mtmd_helper_init_opt_default());
        using Bitmap = std::unique_ptr<mtmd_bitmap, decltype(&mtmd_bitmap_free)>;
        Bitmap bitmap(loaded.bitmap, mtmd_bitmap_free);
        if (loaded.video_ctx) {
            mtmd_helper_video_free(loaded.video_ctx);
            throw std::runtime_error("video must be supplied as frames and audio windows");
        }
        if (!bitmap || mtmd_bitmap_is_audio(bitmap.get()) == image)
            throw std::runtime_error("cannot decode media input");
        if (!image) {
            size_t limit = 30 * mtmd_get_audio_sample_rate(media);
            if (mtmd_bitmap_get_n_bytes(bitmap.get()) / sizeof(float) > limit)
                throw std::runtime_error("audio window must not exceed 30 seconds");
        }
        std::unique_ptr<mtmd_input_chunks, decltype(&mtmd_input_chunks_free)> chunks(
            mtmd_input_chunks_init(), mtmd_input_chunks_free);
        auto raw = bitmap.get();
        const char * marker = mtmd_default_marker();
        mtmd_input_text input{marker, std::strlen(marker), true, true};
        if (mtmd_tokenize(media, chunks.get(), &input, &raw, 1) != 0)
            throw std::runtime_error("cannot tokenize media");
        reset();
        for (size_t i = 0; i < mtmd_input_chunks_size(chunks.get()); ++i) {
            auto chunk = mtmd_input_chunks_get(chunks.get(), i);
            if (mtmd_input_chunk_get_type(chunk) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
                size_t count = 0;
                auto tokens = mtmd_input_chunk_get_tokens_text(chunk, &count);
                for (size_t j = 0; j < count; ++j) add(tokens[j]);
            } else {
                if (mtmd_encode_chunk(media, chunk) != 0) throw std::runtime_error("media encoder failed");
                auto rows = mtmd_get_output_embd(media);
                if (!rows) throw std::runtime_error("media encoder returned no output");
                int width = llama_model_n_embd_inp(model);
                for (size_t j = 0; j < mtmd_input_chunk_get_n_tokens(chunk); ++j) {
                    llama_embd embd{rows + j * width, 1, static_cast<size_t>(width)};
                    place(llama_batch_ext_add_embd(batch, 0, embd));
                }
            }
        }
        return run();
    }
};

int main(int argc, char ** argv) {
    try {
        std::string text, media;
        int threads = std::min(8u, std::max(1u, std::thread::hardware_concurrency()));
        int gpu_layers = 0;
        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i];
            if (i + 1 >= argc) throw std::runtime_error("missing argument value");
            if (arg == "--model") text = argv[++i];
            else if (arg == "--media") media = argv[++i];
            else if (arg == "--threads") threads = std::stoi(argv[++i]);
            else if (arg == "--gpu-layers") gpu_layers = std::stoi(argv[++i]);
            else throw std::runtime_error("unknown argument: " + arg);
        }
        if (text.empty() || threads < 1) throw std::runtime_error("--model and positive threads required");
        llama_log_set(log_callback, nullptr);
        mtmd_helper_log_set(log_callback, nullptr);
        llama_backend_init();
        {
            Encoder encoder(text, media, threads, gpu_layers);
            std::cout << encoder.info().dump() << std::endl;
            std::string line;
            while (std::getline(std::cin, line)) {
                try {
                    auto request = json::parse(line);
                    auto type = request.at("type").get<std::string>();
                    std::vector<float> vector;
                    if (type == "text") vector = encoder.text(request.at("text"));
                    else if (type == "image" || type == "audio") vector = encoder.file(request.at("path"), type == "image");
                    else throw std::runtime_error("unknown embedding type");
                    std::cout << json({{"vector", vector}}).dump() << std::endl;
                } catch (const std::exception & e) {
                    std::cout << json({{"error", e.what()}}).dump() << std::endl;
                }
            }
        }
        llama_backend_free();
        return 0;
    } catch (const std::exception & e) {
        std::cerr << "digup-worker: " << e.what() << std::endl;
        return 1;
    }
}
