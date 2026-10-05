#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <limits>
#include <memory>
#include <span>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace gd_graph {

inline constexpr std::size_t kMaxAtoms = 10'000'000;
inline constexpr std::size_t kMaxEffects = 10'000'000;
inline constexpr std::size_t kMaxObjects = 1'000'000;
inline constexpr std::size_t kMaxEdges = 100'000'000;

struct Header {
    char magic[8];
    std::uint32_t version;
    std::uint32_t objectCount;
    std::uint32_t atomCount;
    std::uint32_t effectCount;
    std::uint64_t rootEdgeCount;
    std::uint64_t producerEdgeCount;
    std::uint64_t inputEdgeCount;
    std::uint64_t consumerEdgeCount;
    std::uint64_t outputEdgeCount;
    std::uint64_t sourceEdgeCount;
};

struct MapFile {
    HANDLE file = INVALID_HANDLE_VALUE;
    HANDLE mapping = nullptr;
    std::byte const* data = nullptr;
    std::size_t size = 0;

    ~MapFile() {
        if (data) UnmapViewOfFile(data);
        if (mapping) CloseHandle(mapping);
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    }
};

struct Query {
    std::vector<std::size_t> orange;
    std::size_t rootAtoms = 0;
    std::size_t reqAtoms = 0;
    std::size_t keptEffects = 0;
};

class Graph {
public:
    static Graph open(
        std::filesystem::path const& path,
        std::size_t expectedObjects = 0
    ) {
        Graph result;
        result.m_file = std::make_shared<MapFile>();
        result.m_file->file = CreateFileW(
            path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
            nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr
        );
        if (result.m_file->file == INVALID_HANDLE_VALUE) {
            throw std::runtime_error("Could not open native system index");
        }
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(result.m_file->file, &size) || size.QuadPart <= 0 ||
            static_cast<std::uint64_t>(size.QuadPart) >
                static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
            throw std::runtime_error("Native system index has an invalid size");
        }
        result.m_file->size = static_cast<std::size_t>(size.QuadPart);
        result.m_file->mapping = CreateFileMappingW(
            result.m_file->file, nullptr, PAGE_READONLY, 0, 0, nullptr
        );
        if (!result.m_file->mapping) {
            throw std::runtime_error("Could not map native system index");
        }
        result.m_file->data = static_cast<std::byte const*>(MapViewOfFile(
            result.m_file->mapping, FILE_MAP_READ, 0, 0, 0
        ));
        if (!result.m_file->data) {
            throw std::runtime_error("Could not view native system index");
        }
        result.load(expectedObjects);
        return result;
    }

    void clear() {
        *this = Graph{};
    }

    Header const& header() const {
        return m_head;
    }

    Query query(std::vector<std::size_t> const& selected) const {
        std::vector<std::uint32_t> roots;
        for (auto object : selected) {
            if (object >= m_head.objectCount) {
                throw std::runtime_error("Selection is outside the indexed level");
            }
            for (auto edge = offsetAt(m_rootOff, object);
                 edge < offsetAt(m_rootOff, object + 1); ++edge) {
                roots.push_back(m_rootAtoms[static_cast<std::size_t>(edge)]);
            }
        }
        auto result = queryAtoms(roots);
        std::vector<std::uint8_t> selectedMarks(m_head.objectCount);
        for (auto object : selected) selectedMarks[object] = 1;
        std::erase_if(result.orange, [&](std::size_t object) {
            return selectedMarks[object] != 0;
        });
        return result;
    }

    Query queryAtoms(std::span<std::uint32_t const> roots) const {
        std::vector<std::uint8_t> atomMarks(m_head.atomCount);
        std::vector<std::uint8_t> effectMarks(m_head.effectCount);
        std::vector<std::uint8_t> objectMarks(m_head.objectCount);
        std::vector<std::uint32_t> pending;
        std::vector<std::size_t> objects;
        pending.reserve(std::min<std::size_t>(roots.size() + 256, m_head.atomCount));
        objects.reserve(std::min<std::size_t>(roots.size() + 256, m_head.objectCount));
        auto addObject = [&](std::int32_t object) {
            if (object < 0) return;
            auto index = static_cast<std::size_t>(object);
            if (index >= m_head.objectCount) {
                throw std::runtime_error("Index names an unknown object");
            }
            if (!objectMarks[index]) {
                objectMarks[index] = 1;
                objects.push_back(index);
            }
        };
        auto addAtom = [&](std::uint32_t atom) {
            if (atom >= m_head.atomCount) {
                throw std::runtime_error("Index names an unknown atom");
            }
            if (!atomMarks[atom]) {
                atomMarks[atom] = 1;
                pending.push_back(atom);
                addObject(m_atomObjs[atom]);
            }
        };
        for (auto atom : roots) addAtom(atom);
        auto rootCount = pending.size();
        std::size_t cursor = 0;
        std::size_t keptEffects = 0;
        while (cursor < pending.size()) {
            auto atom = pending[cursor++];
            for (auto edge = offsetAt(m_prodOff, atom);
                 edge < offsetAt(m_prodOff, atom + 1); ++edge) {
                auto effect = m_prodFx[static_cast<std::size_t>(edge)];
                if (effect >= m_head.effectCount) {
                    throw std::runtime_error("Index names an unknown effect");
                }
                if (effectMarks[effect]) continue;
                effectMarks[effect] = 1;
                ++keptEffects;
                addObject(m_fxSrc[effect]);
                for (auto input = offsetAt(m_inputOff, effect);
                     input < offsetAt(m_inputOff, effect + 1); ++input) {
                    addAtom(m_inputAtoms[static_cast<std::size_t>(input)]);
                }
            }
        }
        return Query{
            std::move(objects), rootCount, pending.size(), keptEffects
        };
    }

    Query querySystem(
        std::span<std::size_t const> selected,
        std::span<std::uint32_t const> targets
    ) const {
        std::vector<std::uint8_t> allowed(m_head.atomCount);
        std::vector<std::uint8_t> atoms(m_head.atomCount);
        std::vector<std::uint8_t> effects(m_head.effectCount);
        std::vector<std::uint8_t> hits(m_head.atomCount);
        std::vector<std::uint8_t> pieces(m_head.objectCount);
        std::vector<std::uint32_t> atomQueue;
        std::vector<std::uint32_t> fxQueue;
        std::vector<std::uint32_t> roots;
        for (auto atom : targets) {
            if (atom >= m_head.atomCount) {
                throw std::runtime_error("System target is outside the index");
            }
            allowed[atom] = 1;
        }
        auto addAtom = [&](std::uint32_t atom) {
            if (atom >= m_head.atomCount) {
                throw std::runtime_error("System path names an unknown atom");
            }
            if (atoms[atom]) return;
            atoms[atom] = 1;
            atomQueue.push_back(atom);
            if (allowed[atom] && !hits[atom]) {
                hits[atom] = 1;
                roots.push_back(atom);
                if (m_atomObjs[atom] >= 0) pieces[m_atomObjs[atom]] = 1;
            }
        };
        auto addEffect = [&](std::uint32_t effect) {
            if (effect >= m_head.effectCount) {
                throw std::runtime_error("System path names an unknown effect");
            }
            if (!effects[effect]) {
                effects[effect] = 1;
                fxQueue.push_back(effect);
            }
        };
        for (auto object : selected) {
            if (object >= m_head.objectCount) {
                throw std::runtime_error("System selection is outside the index");
            }
            for (auto edge = offsetAt(m_rootOff, object);
                 edge < offsetAt(m_rootOff, object + 1); ++edge) {
                addAtom(m_rootAtoms[static_cast<std::size_t>(edge)]);
            }
            for (auto edge = offsetAt(m_srcOff, object);
                 edge < offsetAt(m_srcOff, object + 1); ++edge) {
                addEffect(m_srcFx[static_cast<std::size_t>(edge)]);
            }
        }
        std::size_t atomCursor = 0;
        std::size_t fxCursor = 0;
        while (atomCursor < atomQueue.size() || fxCursor < fxQueue.size()) {
            while (atomCursor < atomQueue.size()) {
                auto atom = atomQueue[atomCursor++];
                for (auto edge = offsetAt(m_consOff, atom);
                     edge < offsetAt(m_consOff, atom + 1); ++edge) {
                    addEffect(m_consFx[static_cast<std::size_t>(edge)]);
                }
            }
            while (fxCursor < fxQueue.size()) {
                auto effect = fxQueue[fxCursor++];
                for (auto edge = offsetAt(m_outOff, effect);
                     edge < offsetAt(m_outOff, effect + 1); ++edge) {
                    addAtom(m_outAtoms[static_cast<std::size_t>(edge)]);
                }
            }
        }
        for (auto atom : targets) {
            auto object = m_atomObjs[atom];
            if (object >= 0 && pieces[object] && !hits[atom]) {
                hits[atom] = 1;
                roots.push_back(atom);
            }
        }
        return queryAtoms(roots);
    }

private:
    static std::uint64_t offsetAt(
        std::byte const* values,
        std::size_t index
    ) {
        std::uint64_t result = 0;
        std::memcpy(&result, values + index * sizeof(result), sizeof(result));
        return result;
    }

    void load(std::size_t expectedObjects) {
        if (m_file->size < sizeof(Header)) {
            throw std::runtime_error("Native system index header is truncated");
        }
        std::memcpy(&m_head, m_file->data, sizeof(m_head));
        if (std::string_view(m_head.magic, 8) != "GDHSIDX2" ||
            m_head.version != 2 ||
            (expectedObjects && m_head.objectCount != expectedObjects)) {
            throw std::runtime_error("Native system index header does not match");
        }
        if (m_head.objectCount > kMaxObjects || m_head.atomCount > kMaxAtoms ||
            m_head.effectCount > kMaxEffects ||
            m_head.rootEdgeCount > kMaxEdges ||
            m_head.producerEdgeCount > kMaxEdges ||
            m_head.inputEdgeCount > kMaxEdges ||
            m_head.consumerEdgeCount > kMaxEdges ||
            m_head.outputEdgeCount > kMaxEdges ||
            m_head.sourceEdgeCount > kMaxEdges) {
            throw std::runtime_error("Native system index exceeds load limits");
        }
        std::size_t cursor = sizeof(Header);
        auto take = [&](std::uint64_t count, std::size_t width) {
            if (count > std::numeric_limits<std::size_t>::max() / width) {
                throw std::runtime_error("Native system index section overflows");
            }
            auto bytes = static_cast<std::size_t>(count) * width;
            if (cursor > m_file->size || bytes > m_file->size - cursor) {
                throw std::runtime_error("Native system index section is truncated");
            }
            auto value = m_file->data + cursor;
            cursor += bytes;
            return value;
        };
        m_rootOff = take(m_head.objectCount + 1ull, sizeof(std::uint64_t));
        m_rootAtoms = reinterpret_cast<std::uint32_t const*>(
            take(m_head.rootEdgeCount, sizeof(std::uint32_t))
        );
        m_atomObjs = reinterpret_cast<std::int32_t const*>(
            take(m_head.atomCount, sizeof(std::int32_t))
        );
        m_prodOff = take(m_head.atomCount + 1ull, sizeof(std::uint64_t));
        m_prodFx = reinterpret_cast<std::uint32_t const*>(
            take(m_head.producerEdgeCount, sizeof(std::uint32_t))
        );
        m_fxSrc = reinterpret_cast<std::int32_t const*>(
            take(m_head.effectCount, sizeof(std::int32_t))
        );
        m_inputOff = take(m_head.effectCount + 1ull, sizeof(std::uint64_t));
        m_inputAtoms = reinterpret_cast<std::uint32_t const*>(
            take(m_head.inputEdgeCount, sizeof(std::uint32_t))
        );
        m_consOff = take(m_head.atomCount + 1ull, sizeof(std::uint64_t));
        m_consFx = reinterpret_cast<std::uint32_t const*>(
            take(m_head.consumerEdgeCount, sizeof(std::uint32_t))
        );
        m_outOff = take(m_head.effectCount + 1ull, sizeof(std::uint64_t));
        m_outAtoms = reinterpret_cast<std::uint32_t const*>(
            take(m_head.outputEdgeCount, sizeof(std::uint32_t))
        );
        m_srcOff = take(m_head.objectCount + 1ull, sizeof(std::uint64_t));
        m_srcFx = reinterpret_cast<std::uint32_t const*>(
            take(m_head.sourceEdgeCount, sizeof(std::uint32_t))
        );
        if (cursor != m_file->size) {
            throw std::runtime_error("Native system index has trailing data");
        }
        validOffsets(m_rootOff, m_head.objectCount, m_head.rootEdgeCount);
        validOffsets(m_prodOff, m_head.atomCount, m_head.producerEdgeCount);
        validOffsets(m_inputOff, m_head.effectCount, m_head.inputEdgeCount);
        validOffsets(m_consOff, m_head.atomCount, m_head.consumerEdgeCount);
        validOffsets(m_outOff, m_head.effectCount, m_head.outputEdgeCount);
        validOffsets(m_srcOff, m_head.objectCount, m_head.sourceEdgeCount);
        for (std::uint64_t index = 0; index < m_head.rootEdgeCount; ++index) {
            if (m_rootAtoms[index] >= m_head.atomCount) {
                throw std::runtime_error("Native system index has an invalid root");
            }
        }
        for (std::uint64_t index = 0; index < m_head.producerEdgeCount; ++index) {
            if (m_prodFx[index] >= m_head.effectCount) {
                throw std::runtime_error("Native system index has an invalid producer");
            }
        }
        for (std::uint64_t index = 0; index < m_head.inputEdgeCount; ++index) {
            if (m_inputAtoms[index] >= m_head.atomCount) {
                throw std::runtime_error("Native system index has an invalid input");
            }
        }
        for (std::uint64_t index = 0; index < m_head.consumerEdgeCount; ++index) {
            if (m_consFx[index] >= m_head.effectCount) {
                throw std::runtime_error("Native system index has an invalid consumer");
            }
        }
        for (std::uint64_t index = 0; index < m_head.outputEdgeCount; ++index) {
            if (m_outAtoms[index] >= m_head.atomCount) {
                throw std::runtime_error("Native system index has an invalid output");
            }
        }
        for (std::uint64_t index = 0; index < m_head.sourceEdgeCount; ++index) {
            if (m_srcFx[index] >= m_head.effectCount) {
                throw std::runtime_error("Native system index has an invalid source");
            }
        }
    }

    static void validOffsets(
        std::byte const* values,
        std::size_t count,
        std::uint64_t edgeCount
    ) {
        if (offsetAt(values, 0) != 0 || offsetAt(values, count) != edgeCount) {
            throw std::runtime_error("Native system index offsets are invalid");
        }
        for (std::size_t index = 0; index < count; ++index) {
            if (offsetAt(values, index) > offsetAt(values, index + 1)) {
                throw std::runtime_error("Native system index offsets are unordered");
            }
        }
    }

    std::shared_ptr<MapFile> m_file;
    Header m_head{};
    std::byte const* m_rootOff = nullptr;
    std::uint32_t const* m_rootAtoms = nullptr;
    std::int32_t const* m_atomObjs = nullptr;
    std::byte const* m_prodOff = nullptr;
    std::uint32_t const* m_prodFx = nullptr;
    std::int32_t const* m_fxSrc = nullptr;
    std::byte const* m_inputOff = nullptr;
    std::uint32_t const* m_inputAtoms = nullptr;
    std::byte const* m_consOff = nullptr;
    std::uint32_t const* m_consFx = nullptr;
    std::byte const* m_outOff = nullptr;
    std::uint32_t const* m_outAtoms = nullptr;
    std::byte const* m_srcOff = nullptr;
    std::uint32_t const* m_srcFx = nullptr;
};

}
