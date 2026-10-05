#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>

#include <native_sem.hpp>
#include "native_graph.hpp"

#include <algorithm>
#include <array>
#include <bitset>
#include <charconv>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

constexpr std::uint8_t kKnown = 1;
constexpr std::uint8_t kRoot = 2;
constexpr std::uint8_t kNoTouch = 32;

class WinMap {
public:
    explicit WinMap(std::filesystem::path const& path) {
        m_file = CreateFileW(
            path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr
        );
        if (m_file == INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot open input");
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(m_file, &size) || size.QuadPart <= 0 ||
            static_cast<unsigned long long>(size.QuadPart) >
                std::numeric_limits<std::size_t>::max()) {
            throw std::runtime_error("Invalid input size");
        }
        m_size = static_cast<std::size_t>(size.QuadPart);
        m_map = CreateFileMappingW(m_file, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (!m_map) throw std::runtime_error("Cannot map input");
        m_data = static_cast<char const*>(MapViewOfFile(m_map, FILE_MAP_READ, 0, 0, 0));
        if (!m_data) throw std::runtime_error("Cannot view input");
    }

    WinMap(WinMap const&) = delete;
    WinMap& operator=(WinMap const&) = delete;

    ~WinMap() {
        if (m_data) UnmapViewOfFile(m_data);
        if (m_map) CloseHandle(m_map);
        if (m_file != INVALID_HANDLE_VALUE) CloseHandle(m_file);
    }

    std::string_view text() const {
        return {m_data, m_size};
    }

private:
    HANDLE m_file = INVALID_HANDLE_VALUE;
    HANDLE m_map = nullptr;
    char const* m_data = nullptr;
    std::size_t m_size = 0;
};

class Sha256 {
public:
    Sha256() {
        if (BCryptOpenAlgorithmProvider(&m_alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0) {
            throw std::runtime_error("Cannot open SHA-256");
        }
        DWORD bytes = 0;
        DWORD size = 0;
        if (BCryptGetProperty(
                m_alg, BCRYPT_OBJECT_LENGTH,
                reinterpret_cast<PUCHAR>(&size), sizeof(size), &bytes, 0
            ) < 0) {
            throw std::runtime_error("Cannot size SHA-256");
        }
        m_obj.resize(size);
        if (BCryptCreateHash(
                m_alg, &m_hash, m_obj.data(),
                static_cast<ULONG>(m_obj.size()), nullptr, 0, 0
            ) < 0) {
            throw std::runtime_error("Cannot create SHA-256");
        }
    }

    Sha256(Sha256 const&) = delete;
    Sha256& operator=(Sha256 const&) = delete;

    ~Sha256() {
        if (m_hash) BCryptDestroyHash(m_hash);
        if (m_alg) BCryptCloseAlgorithmProvider(m_alg, 0);
    }

    void add(void const* data, std::size_t size) {
        auto bytes = static_cast<PUCHAR>(const_cast<void*>(data));
        while (size) {
            auto part = static_cast<ULONG>(std::min<std::size_t>(
                size, std::numeric_limits<ULONG>::max()
            ));
            if (BCryptHashData(m_hash, bytes, part, 0) < 0) {
                throw std::runtime_error("Cannot update SHA-256");
            }
            bytes += part;
            size -= part;
        }
    }

    std::string done() {
        std::array<unsigned char, 32> digest{};
        if (BCryptFinishHash(m_hash, digest.data(), digest.size(), 0) < 0) {
            throw std::runtime_error("Cannot finish SHA-256");
        }
        constexpr char digits[] = "0123456789ABCDEF";
        std::string value;
        value.resize(digest.size() * 2);
        for (std::size_t index = 0; index < digest.size(); ++index) {
            value[index * 2] = digits[digest[index] >> 4];
            value[index * 2 + 1] = digits[digest[index] & 15];
        }
        return value;
    }

private:
    BCRYPT_ALG_HANDLE m_alg = nullptr;
    BCRYPT_HASH_HANDLE m_hash = nullptr;
    std::vector<unsigned char> m_obj;
};

struct Prop {
    std::uint32_t off;
    std::uint32_t len;
    std::uint16_t key;
};

struct Obj {
    std::uint32_t off;
    std::uint32_t len;
    std::uint32_t prop;
    std::uint16_t count;
    std::uint16_t id;
};

struct ScanOut {
    std::vector<Obj> objects;
    std::vector<Prop> props;
    std::vector<std::uint32_t> roots;
    std::string sourceHash;
    std::string rootHash;
    std::uint64_t sourceSize = 0;
    std::uint64_t settingsLen = 0;
    double parseMs = 0.0;
};

std::int64_t gdAtoi(std::string_view value) {
    std::size_t index = 0;
    while (index < value.size() &&
           (value[index] == ' ' || value[index] == '\t' || value[index] == '\n' ||
            value[index] == '\v' || value[index] == '\f' || value[index] == '\r')) {
        ++index;
    }
    bool negative = false;
    if (index < value.size() && (value[index] == '+' || value[index] == '-')) {
        negative = value[index] == '-';
        ++index;
    }
    std::int64_t result = 0;
    bool found = false;
    while (index < value.size() && value[index] >= '0' && value[index] <= '9') {
        found = true;
        auto digit = static_cast<unsigned>(value[index] - '0');
        if (result > (std::numeric_limits<std::int64_t>::max() - digit) / 10) {
            return negative ? std::numeric_limits<std::int64_t>::min() :
                              std::numeric_limits<std::int64_t>::max();
        }
        result = result * 10 + digit;
        ++index;
    }
    if (!found) return 0;
    return negative ? -result : result;
}

std::int64_t fullInt(std::string_view value) {
    std::size_t begin = 0;
    std::size_t end = value.size();
    while (begin < end && std::isspace(static_cast<unsigned char>(value[begin]))) ++begin;
    while (end > begin && std::isspace(static_cast<unsigned char>(value[end - 1]))) --end;
    bool negative = false;
    if (begin < end && (value[begin] == '+' || value[begin] == '-')) {
        negative = value[begin] == '-';
        ++begin;
    }
    if (begin == end) throw std::runtime_error("Invalid integer property");
    std::uint64_t result = 0;
    for (std::size_t index = begin; index < end; ++index) {
        if (value[index] < '0' || value[index] > '9') {
            throw std::runtime_error("Invalid integer property");
        }
        auto digit = static_cast<unsigned>(value[index] - '0');
        if (result > (std::numeric_limits<std::uint64_t>::max() - digit) / 10) {
            throw std::runtime_error("Integer property overflow");
        }
        result = result * 10 + digit;
    }
    if ((!negative && result > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) ||
        (negative && result > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) + 1)) {
        throw std::runtime_error("Integer property overflow");
    }
    if (!negative) return static_cast<std::int64_t>(result);
    if (result == static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) + 1) {
        return std::numeric_limits<std::int64_t>::min();
    }
    return -static_cast<std::int64_t>(result);
}

std::string jsonText(std::string_view value) {
    std::string result;
    result.push_back('"');
    constexpr char digits[] = "0123456789ABCDEF";
    for (unsigned char character : value) {
        if (character == '"' || character == '\\') {
            result.push_back('\\');
            result.push_back(static_cast<char>(character));
        } else if (character == '\b') result.append("\\b");
        else if (character == '\f') result.append("\\f");
        else if (character == '\n') result.append("\\n");
        else if (character == '\r') result.append("\\r");
        else if (character == '\t') result.append("\\t");
        else if (character < 32) {
            result.append("\\u00");
            result.push_back(digits[character >> 4]);
            result.push_back(digits[character & 15]);
        } else result.push_back(static_cast<char>(character));
    }
    result.push_back('"');
    return result;
}

ScanOut scanLevel(std::string_view raw) {
    auto started = std::chrono::steady_clock::now();
    if (raw.size() > std::numeric_limits<std::uint32_t>::max()) {
        throw std::runtime_error("Level input exceeds native index offsets");
    }
    auto first = raw.find(';');
    if (first == std::string_view::npos || first == 0) {
        throw std::runtime_error("Level settings are missing");
    }
    ScanOut output;
    output.sourceSize = raw.size();
    output.settingsLen = first;
    std::size_t position = first + 1;
    std::array<std::uint32_t, gd_native::kProps.size()> offsets{};
    std::array<std::uint32_t, gd_native::kProps.size()> lengths{};
    std::bitset<gd_native::kProps.size()> present;
    while (position <= raw.size()) {
        auto finish = raw.find(';', position);
        if (finish == std::string_view::npos) finish = raw.size();
        auto record = raw.substr(position, finish - position);
        if (!record.empty()) {
            present.reset();
            std::size_t token = 0;
            while (token < record.size()) {
                auto keyEnd = record.find(',', token);
                if (keyEnd == std::string_view::npos) break;
                auto valStart = keyEnd + 1;
                auto valEnd = record.find(',', valStart);
                if (valEnd == std::string_view::npos) valEnd = record.size();
                auto key = gdAtoi(record.substr(token, keyEnd - token));
                if (key > 600) throw std::runtime_error("Property exceeds GD 2.2081");
                if (key > 0) {
                    auto slot = gd_native::kPropSlots[static_cast<std::size_t>(key)];
                    if (slot >= 0) {
                        auto index = static_cast<std::size_t>(slot);
                        offsets[index] = static_cast<std::uint32_t>(
                            position + valStart
                        );
                        lengths[index] = static_cast<std::uint32_t>(valEnd - valStart);
                        present.set(index);
                    }
                }
                if (valEnd == record.size()) break;
                token = valEnd + 1;
            }
            auto idSlot = static_cast<std::size_t>(gd_native::kPropSlots[1]);
            auto objectId = present.test(idSlot) ? gdAtoi(raw.substr(
                offsets[idSlot], lengths[idSlot]
            )) : 0;
            if (objectId <= 0) {
                throw std::runtime_error("Level record has no effective object ID");
            }
            if (static_cast<std::size_t>(objectId) >= gd_native::kAssets.size() ||
                !(gd_native::kAssets[static_cast<std::size_t>(objectId)].flags & kKnown)) {
                throw std::runtime_error("Unknown object ID " + std::to_string(objectId));
            }
            auto propStart = static_cast<std::uint32_t>(output.props.size());
            for (std::size_t index = 0; index < gd_native::kProps.size(); ++index) {
                if (present.test(index)) {
                    output.props.push_back(Prop{
                        offsets[index], lengths[index], gd_native::kProps[index]
                    });
                }
            }
            output.objects.push_back(Obj{
                static_cast<std::uint32_t>(position),
                static_cast<std::uint32_t>(record.size()),
                propStart,
                static_cast<std::uint16_t>(output.props.size() - propStart),
                static_cast<std::uint16_t>(objectId),
            });
            auto const flags = gd_native::kAssets[static_cast<std::size_t>(objectId)].flags;
            bool root = flags & kRoot;
            if (root && (flags & kNoTouch)) {
                auto touchSlot = static_cast<std::size_t>(gd_native::kPropSlots[121]);
                if (present.test(touchSlot)) {
                    root = fullInt(raw.substr(
                        offsets[touchSlot], lengths[touchSlot]
                    )) == 0;
                }
            }
            if (root) {
                output.roots.push_back(
                    static_cast<std::uint32_t>(output.objects.size() - 1)
                );
            }
        }
        if (finish == raw.size()) break;
        position = finish + 1;
    }
    Sha256 sourceHash;
    sourceHash.add(raw.data(), raw.size());
    output.sourceHash = sourceHash.done();
    Sha256 rootHash;
    for (auto index : output.roots) rootHash.add(&index, sizeof(index));
    output.rootHash = rootHash.done();
    output.parseMs = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - started
    ).count();
    return output;
}

std::array<unsigned char, 32> hashBytes(std::string_view value) {
    if (value.size() != 64) throw std::runtime_error("Invalid SHA-256 text");
    std::array<unsigned char, 32> result{};
    auto digit = [](char value) -> unsigned char {
        if (value >= '0' && value <= '9') return value - '0';
        if (value >= 'A' && value <= 'F') return value - 'A' + 10;
        if (value >= 'a' && value <= 'f') return value - 'a' + 10;
        throw std::runtime_error("Invalid SHA-256 text");
    };
    for (std::size_t index = 0; index < result.size(); ++index) {
        result[index] = static_cast<unsigned char>(
            digit(value[index * 2]) * 16 + digit(value[index * 2 + 1])
        );
    }
    return result;
}

class IndexOut {
public:
    explicit IndexOut(std::filesystem::path path) : m_path(std::move(path)) {
        m_temp = m_path;
        m_temp += L".tmp";
        std::filesystem::create_directories(m_path.parent_path());
        m_stream.open(m_temp, std::ios::binary | std::ios::trunc);
        if (!m_stream) throw std::runtime_error("Cannot create native index");
    }

    void add(void const* data, std::size_t size) {
        m_stream.write(static_cast<char const*>(data), static_cast<std::streamsize>(size));
        if (!m_stream) throw std::runtime_error("Cannot write native index");
        m_hash.add(data, size);
        m_size += size;
    }

    template <class Value>
    void number(Value value) {
        add(&value, sizeof(value));
    }

    std::pair<std::string, std::uint64_t> finish() {
        m_stream.close();
        if (!m_stream) throw std::runtime_error("Cannot close native index");
        if (!MoveFileExW(
                m_temp.c_str(), m_path.c_str(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            )) {
            throw std::runtime_error("Cannot publish native index");
        }
        return {m_hash.done(), m_size};
    }

private:
    std::filesystem::path m_path;
    std::filesystem::path m_temp;
    std::ofstream m_stream;
    Sha256 m_hash;
    std::uint64_t m_size = 0;
};

std::pair<std::string, std::uint64_t> writeIndex(
    std::filesystem::path const& path,
    ScanOut const& value
) {
    IndexOut output(path);
    constexpr std::array<char, 8> magic{'G','D','L','I','D','X','0','2'};
    output.add(magic.data(), magic.size());
    output.number<std::uint32_t>(2);
    output.number<std::uint32_t>(gd_native::kSemRev);
    output.number<std::uint64_t>(value.settingsLen);
    output.number<std::uint64_t>(value.objects.size());
    output.number<std::uint64_t>(value.props.size());
    output.number<std::uint64_t>(value.roots.size());
    output.number<std::uint64_t>(value.sourceSize);
    auto sourceHash = hashBytes(value.sourceHash);
    auto classHash = hashBytes(gd_native::kClassHash);
    output.add(sourceHash.data(), sourceHash.size());
    output.add(classHash.data(), classHash.size());
    for (auto const& object : value.objects) {
        output.number(object.off);
        output.number(object.len);
        output.number(object.prop);
        output.number(object.count);
        output.number(object.id);
    }
    for (auto const& prop : value.props) {
        output.number(prop.off);
        output.number(prop.len);
        output.number(prop.key);
        output.number<std::uint16_t>(0);
    }
    for (auto root : value.roots) output.number(root);
    return output.finish();
}

struct SeedData {
    std::uint32_t objects;
    std::uint32_t pieces;
    std::span<std::uint32_t const> roots;
};

SeedData loadSeeds(std::string_view raw) {
    if (raw.size() < 32 || raw.substr(0, 8) != "GDLSEED1") {
        throw std::runtime_error("Native closure seeds are invalid");
    }
    auto read32 = [&](std::size_t offset) {
        std::uint32_t value = 0;
        std::memcpy(&value, raw.data() + offset, sizeof(value));
        return value;
    };
    auto read64 = [&](std::size_t offset) {
        std::uint64_t value = 0;
        std::memcpy(&value, raw.data() + offset, sizeof(value));
        return value;
    };
    auto version = read32(8);
    auto objects = read32(12);
    auto pieces = read32(16);
    auto reserved = read32(20);
    auto count = read64(24);
    if (version != 1 || reserved != 0 ||
        count > (raw.size() - 32) / sizeof(std::uint32_t) ||
        raw.size() != 32 + count * sizeof(std::uint32_t)) {
        throw std::runtime_error("Native closure seed table is invalid");
    }
    return SeedData{
        objects, pieces,
        {reinterpret_cast<std::uint32_t const*>(raw.data() + 32),
         static_cast<std::size_t>(count)}
    };
}

struct CloseOut {
    std::string fileHash;
    std::string indexHash;
    std::uint64_t fileSize;
    std::size_t keptObjects;
};

CloseOut writeClose(
    std::filesystem::path const& path,
    std::uint32_t objectCount,
    std::vector<std::size_t> objects
) {
    std::ranges::sort(objects);
    objects.erase(std::unique(objects.begin(), objects.end()), objects.end());
    IndexOut output(path);
    constexpr std::array<char, 8> magic{'G','D','L','C','L','O','S','E'};
    output.add(magic.data(), magic.size());
    output.number<std::uint32_t>(1);
    output.number<std::uint32_t>(objectCount);
    output.number<std::uint64_t>(objects.size());
    Sha256 indexHash;
    for (auto object : objects) {
        if (object >= objectCount ||
            object > std::numeric_limits<std::uint32_t>::max()) {
            throw std::runtime_error("Native closure produced an invalid object");
        }
        auto value = static_cast<std::uint32_t>(object);
        output.number(value);
        indexHash.add(&value, sizeof(value));
    }
    auto [fileHash, fileSize] = output.finish();
    return CloseOut{
        std::move(fileHash), indexHash.done(), fileSize, objects.size()
    };
}

void writeFile(std::filesystem::path const& path, std::string const& value) {
    auto temp = path;
    temp += L".tmp";
    std::filesystem::create_directories(path.parent_path());
    {
        std::ofstream stream(temp, std::ios::binary | std::ios::trunc);
        if (!stream) throw std::runtime_error("Cannot create output");
        stream.write(value.data(), static_cast<std::streamsize>(value.size()));
        if (!stream) throw std::runtime_error("Cannot write output");
    }
    if (!MoveFileExW(
            temp.c_str(), path.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        )) {
        throw std::runtime_error("Cannot publish output");
    }
}

std::string successJson(
    ScanOut const& value,
    std::string_view indexHash,
    std::uint64_t indexSize
) {
    return "{\n  \"schema\": \"gd-strip-native-stage-2\",\n"
        "  \"schema_major\": 1,\n"
        "  \"success\": true,\n"
        "  \"classifier_sha256\": " + jsonText(gd_native::kClassHash) + ",\n"
        "  \"source_sha256\": " + jsonText(value.sourceHash) + ",\n"
        "  \"object_count\": " + std::to_string(value.objects.size()) + ",\n"
        "  \"property_count\": " + std::to_string(value.props.size()) + ",\n"
        "  \"true_layout_count\": " + std::to_string(value.roots.size()) + ",\n"
        "  \"root_index_sha256\": " + jsonText(value.rootHash) + ",\n"
        "  \"index_sha256\": " + jsonText(indexHash) + ",\n"
        "  \"index_size_bytes\": " + std::to_string(indexSize) + ",\n"
        "  \"parse_ms\": " + std::to_string(value.parseMs) + "\n}\n";
}

std::string failureJson(std::string_view error) {
    return "{\n  \"schema\": \"gd-strip-native-stage-2\",\n"
        "  \"schema_major\": 1,\n"
        "  \"success\": false,\n"
        "  \"classifier_sha256\": " + jsonText(gd_native::kClassHash) + ",\n"
        "  \"error\": " + jsonText(error) + "\n}\n";
}

std::string closeJson(
    gd_graph::Query const& query,
    SeedData const& seeds,
    CloseOut const& output,
    double queryMs
) {
    return "{\n  \"schema\": \"gd-strip-native-close\",\n"
        "  \"schema_major\": 1,\n"
        "  \"success\": true,\n"
        "  \"classifier_sha256\": " + jsonText(gd_native::kClassHash) + ",\n"
        "  \"object_count\": " + std::to_string(seeds.objects) + ",\n"
        "  \"piece_count\": " + std::to_string(seeds.pieces) + ",\n"
        "  \"seed_atom_count\": " + std::to_string(seeds.roots.size()) + ",\n"
        "  \"root_atom_count\": " + std::to_string(query.rootAtoms) + ",\n"
        "  \"required_atom_count\": " + std::to_string(query.reqAtoms) + ",\n"
        "  \"retained_effect_count\": " + std::to_string(query.keptEffects) + ",\n"
        "  \"retained_object_count\": " + std::to_string(output.keptObjects) + ",\n"
        "  \"kept_index_sha256\": " + jsonText(output.indexHash) + ",\n"
        "  \"kept_file_sha256\": " + jsonText(output.fileHash) + ",\n"
        "  \"kept_size_bytes\": " + std::to_string(output.fileSize) + ",\n"
        "  \"query_ms\": " + std::to_string(queryMs) + "\n}\n";
}

std::string closeFail(std::string_view error) {
    return "{\n  \"schema\": \"gd-strip-native-close\",\n"
        "  \"schema_major\": 1,\n"
        "  \"success\": false,\n"
        "  \"classifier_sha256\": " + jsonText(gd_native::kClassHash) + ",\n"
        "  \"error\": " + jsonText(error) + "\n}\n";
}

}

int wmain(int argc, wchar_t** argv) {
    if (argc < 2) return 64;
    auto mode = std::wstring_view(argv[1]);
    if (mode == L"parse" && argc == 5) {
        auto index = std::filesystem::path(argv[3]);
        auto output = std::filesystem::path(argv[4]);
        try {
            WinMap input{std::filesystem::path(argv[2])};
            auto result = scanLevel(input.text());
            auto [indexHash, indexSize] = writeIndex(index, result);
            writeFile(output, successJson(result, indexHash, indexSize));
            return 0;
        } catch (std::exception const& error) {
            try {
                writeFile(output, failureJson(error.what()));
            } catch (...) {
            }
            return 1;
        }
    }
    if ((mode == L"close" && argc == 6) ||
        (mode == L"system" && argc == 7)) {
        auto output = std::filesystem::path(argv[4]);
        auto report = std::filesystem::path(argv[5]);
        try {
            WinMap seedMap{std::filesystem::path(argv[3])};
            auto seeds = loadSeeds(seedMap.text());
            auto graph = gd_graph::Graph::open(
                std::filesystem::path(argv[2]), seeds.objects
            );
            auto started = std::chrono::steady_clock::now();
            std::vector<std::size_t> selected;
            if (mode == L"system") {
                auto text = std::wstring_view(argv[6]);
                while (!text.empty()) {
                    auto end = text.find(L',');
                    auto token = text.substr(0, end);
                    std::size_t used = 0;
                    auto index = std::stoull(std::wstring(token), &used);
                    if (used != token.size()) {
                        throw std::runtime_error("System selection is invalid");
                    }
                    selected.push_back(static_cast<std::size_t>(index));
                    if (end == std::wstring_view::npos) break;
                    text.remove_prefix(end + 1);
                }
            }
            auto query = mode == L"system"
                ? graph.querySystem(selected, seeds.roots)
                : graph.queryAtoms(seeds.roots);
            auto queryMs = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - started
            ).count();
            auto kept = writeClose(
                output, seeds.objects, std::move(query.orange)
            );
            writeFile(report, closeJson(query, seeds, kept, queryMs));
            return 0;
        } catch (std::exception const& error) {
            try {
                writeFile(report, closeFail(error.what()));
            } catch (...) {
            }
            return 1;
        }
    }
    return 64;
}
