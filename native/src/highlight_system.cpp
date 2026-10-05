#include "highlight_system.hpp"
#include "native_graph.hpp"
#include "deco_toggle.hpp"
#include <native_sem.hpp>

#include <Geode/modify/AppDelegate.hpp>
#include <Geode/modify/CCSprite.hpp>
#include <Geode/modify/EditorUI.hpp>
#include <Geode/modify/GameObject.hpp>
#include <Geode/modify/LevelEditorLayer.hpp>
#include <Geode/utils/cocos.hpp>
#include <Geode/utils/file.hpp>
#include <Geode/utils/hash.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <vector>
#include <unordered_set>
#include <unordered_map>

#ifdef GEODE_IS_WINDOWS
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

using namespace geode::prelude;

namespace gd_strip {
namespace {

constexpr cocos2d::ccColor3B kOrange{255, 128, 0};
std::vector<geode::Ref<GameObject>> s_orange;
std::unordered_set<GameObject*> s_native;
std::unordered_set<GameObject*> s_drawn;
std::unordered_map<CCSprite*, GameObject*> s_parts;
std::vector<geode::Ref<CCSprite>> s_sprites;
std::vector<ccColor3B> s_colors;
bool s_drawDirty = false;
LevelEditorLayer* s_editor = nullptr;
std::size_t s_planned = 0;
std::size_t s_excluded = 0;
std::size_t s_painted = 0;
gd_graph::Graph s_graph;
std::vector<std::uint32_t> s_roots;
std::string s_graphHash;

void writeState();

GameObject* partOwner(CCSprite* sprite) {
    auto found = s_parts.find(sprite);
    return found == s_parts.end() ? nullptr : found->second;
}

void recordDraw(cocos2d::CCSprite* sprite) {
    auto* object = partOwner(sprite);
    if (object && !sprite->getDontDraw() &&
        s_drawn.insert(object).second) s_drawDirty = true;
}

bool isOrange(GameObject* object) {
    return object && s_editor && LevelEditorLayer::get() == s_editor &&
        s_editor->m_playbackMode == PlaybackMode::Not &&
        s_native.contains(object) && !object->m_isSelected;
}

void addParts(CCNode* node, GameObject* owner) {
    if (auto* sprite = typeinfo_cast<CCSprite*>(node)) {
        if (s_parts.emplace(sprite, owner).second) {
            s_sprites.emplace_back(sprite);
            s_colors.push_back(sprite->getColor());
        }
    }
    if (auto* children = node->getChildren()) {
        for (auto* child : CCArrayExt<CCNode*>(children)) addParts(child, owner);
    }
}

void paintObject(GameObject* object) {
    if (!isOrange(object)) return;
    auto selected = object->m_isSelected;
    object->selectObject(kOrange);
    object->m_isSelected = selected;
    if (auto* glow = object->m_glowSprite) glow->setColor(kOrange);
    ++s_painted;
}

std::string copyString(gd::string const& value) {
    return std::string(value.c_str(), value.size());
}

std::int64_t unixMillis() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()
    ).count();
}

std::string lowerASCII(std::string value) {
    std::ranges::transform(value, value.begin(), [](unsigned char byte) {
        return static_cast<char>(std::tolower(byte));
    });
    return value;
}

Result<std::vector<std::uint32_t>> readSeeds(
    std::filesystem::path const& path,
    std::size_t objects
) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream) return Err("Causal root file is unavailable");
    auto end = stream.tellg();
    if (end < 32) return Err("Causal root file is truncated");
    std::vector<char> raw(static_cast<std::size_t>(end));
    stream.seekg(0);
    stream.read(raw.data(), static_cast<std::streamsize>(raw.size()));
    if (!stream) return Err("Causal root file could not be read");
    auto read32 = [&raw](std::size_t offset) {
        std::uint32_t value = 0;
        std::memcpy(&value, raw.data() + offset, sizeof(value));
        return value;
    };
    auto read64 = [&raw](std::size_t offset) {
        std::uint64_t value = 0;
        std::memcpy(&value, raw.data() + offset, sizeof(value));
        return value;
    };
    auto count = read64(24);
    if (std::string_view(raw.data(), 8) != "GDLSEED1" ||
        read32(8) != 1 || read32(12) != objects || read32(20) != 0 ||
        count > (raw.size() - 32) / sizeof(std::uint32_t) ||
        raw.size() != 32 + count * sizeof(std::uint32_t)) {
        return Err("Causal root file does not match this level");
    }
    std::vector<std::uint32_t> roots(static_cast<std::size_t>(count));
    if (!roots.empty()) {
        std::memcpy(roots.data(), raw.data() + 32,
                    roots.size() * sizeof(std::uint32_t));
    }
    return Ok(std::move(roots));
}

std::set<GameObject*> userSelected() {
    std::set<GameObject*> selected;
    auto* ui = EditorUI::get();
    if (!ui) return selected;
    if (auto* objects = ui->getSelectedObjects()) {
        for (auto* object : geode::cocos::CCArrayExt<GameObject*>(objects)) {
            if (object) selected.insert(object);
        }
    }
    if (ui->m_selectedObject) selected.insert(ui->m_selectedObject);
    return selected;
}

std::string groupText(GameObject* object) {
    fmt::memory_buffer text;
    bool first = true;
    for (int index = 0; index < 10; ++index) {
        auto group = object->getGroupID(index);
        if (group <= 0) continue;
        if (!first) fmt::format_to(std::back_inserter(text), ".");
        fmt::format_to(std::back_inserter(text), "{}", group);
        first = false;
    }
    return fmt::to_string(text);
}

void writeState() {
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-selection-state";
    root["schema_major"] = 2;
    root["updated_unix_ms"] = unixMillis();
#ifdef GEODE_IS_WINDOWS
    root["game_pid"] = static_cast<std::int64_t>(GetCurrentProcessId());
#else
    root["game_pid"] = 0;
#endif
    auto* editor = LevelEditorLayer::get();
    auto selected = userSelected();
    root["state"] = editor ? "editor_ready" : "no_editor";
    root["level_name"] = editor && editor->m_level
        ? matjson::Value(copyString(editor->m_level->m_levelName))
        : matjson::Value(nullptr);
    root["selected_count"] = static_cast<std::int64_t>(selected.size());
    root["selection_truncated"] = selected.size() > 64;
    root["selected"] = matjson::Value::array();
    std::size_t sampled = 0;
    for (auto* object : selected) {
        if (sampled++ == 64) break;
        auto row = matjson::Value::object();
        row["object_id"] = static_cast<std::int64_t>(object->m_objectID);
        row["runtime_type"] = static_cast<std::int64_t>(object->m_objectType);
        row["is_trigger"] = object->isTrigger();
        row["is_speed_object"] = object->isSpeedObject();
        row["is_special_object"] = object->isSpecialObject();
        auto position = object->getPosition();
        row["x"] = position.x;
        row["y"] = position.y;
        row["rotation"] = object->getRotation();
        row["scale_x"] = object->m_scaleX;
        row["scale_y"] = object->m_scaleY;
        row["editor_layer_1"] = static_cast<std::int64_t>(object->m_editorLayer);
        row["editor_layer_2"] = static_cast<std::int64_t>(object->m_editorLayer2);
        row["z_layer"] = static_cast<std::int64_t>(object->m_zLayer);
        row["z_order"] = static_cast<std::int64_t>(object->m_zOrder);
        row["group_count"] = static_cast<std::int64_t>(object->m_groupCount);
        row["group_string"] = groupText(object);
        if (editor) row["save_string"] = copyString(object->getSaveString(editor));
        root["selected"].push(std::move(row));
    }
    root["orange"]["active"] = !s_orange.empty();
    root["orange"]["assigned_count"] =
        static_cast<std::int64_t>(s_orange.size());
    root["orange"]["drawn_count"] = static_cast<std::int64_t>(s_drawn.size());
    root["orange"]["sprite_count"] = static_cast<std::int64_t>(s_sprites.size());
    std::size_t visible = 0;
    std::size_t inView = 0;
    auto size = CCDirector::get()->getWinSize();
    for (auto const& object : s_orange) {
        bool shown = true;
        for (auto* node = static_cast<CCNode*>(object.data()); node;
             node = node->getParent()) shown &= node->isVisible();
        visible += shown;
        auto point = object->convertToWorldSpace(object->getAnchorPointInPoints());
        inView += shown && point.x >= 0 && point.y >= 0 &&
            point.x <= size.width && point.y <= size.height;
    }
    root["orange"]["visible_count"] = static_cast<std::int64_t>(visible);
    root["orange"]["in_view_count"] = static_cast<std::int64_t>(inView);
    root["orange"]["planned_count"] =
        static_cast<std::int64_t>(s_planned);
    root["orange"]["render_mode"] = "native_selection";
    root["orange"]["native_path"] = "GameObject::selectObject";
    root["orange"]["rgb"] = matjson::Value::array();
    for (auto value : {255, 128, 0}) root["orange"]["rgb"].push(value);
    root["orange"]["selected_priority_count"] =
        static_cast<std::int64_t>(s_excluded);
    root["orange"]["paint_calls"] = static_cast<std::int64_t>(s_painted);
    root["orange"]["asset_style_changed"] = false;
    root["orange"]["samples"] = matjson::Value::array();
    for (std::size_t index = 0; index < std::min<std::size_t>(s_orange.size(), 8); ++index) {
        auto* object = s_orange[index].data();
        auto row = matjson::Value::object();
        auto color = object->getColor();
        row["asset_id"] = object->m_objectID;
        row["red"] = color.r;
        row["green"] = color.g;
        row["blue"] = color.b;
        row["selected"] = object->m_isSelected;
        row["visible"] = object->isVisible();
        row["opacity"] = object->getOpacity();
        row["dont_draw"] = object->getDontDraw();
        row["draw_seen"] = s_drawn.contains(object);
        auto quad = object->getQuad();
        row["quad_rgba"] = matjson::Value::array();
        for (auto value : {quad.bl.colors.r, quad.bl.colors.g,
                          quad.bl.colors.b, quad.bl.colors.a})
            row["quad_rgba"].push(value);
        auto point = object->convertToWorldSpace(object->getAnchorPointInPoints());
        row["screen_x"] = point.x;
        row["screen_y"] = point.y;
        root["orange"]["samples"].push(std::move(row));
    }
    root["preview"] = gd_strip::lightState();
    auto path = Mod::get()->getSaveDir() / "selection-state.json";
    auto written = geode::utils::file::writeStringSafe(path, root.dump());
    if (!written) log::error("Selection state write failed: {}", written.unwrapErr());
}

}

Result<std::size_t> applySysLight(
    std::vector<GameObject*> const& objects,
    std::size_t planned
) {
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_editorUI) {
        return Err("Open a level editor before previewing selected parts");
    }
    clearSysLight();
    s_editor = editor;
    s_planned = planned;
    auto selected = userSelected();
    std::set<GameObject*> seen;
    s_orange.reserve(objects.size());
    s_native.reserve(objects.size());
    for (auto* object : objects) {
        if (!object || !seen.insert(object).second) continue;
        if (selected.contains(object)) {
            ++s_excluded;
            continue;
        }
        s_orange.emplace_back(object);
        s_native.insert(object);
        addParts(object, object);
        if (object->m_colorSprite) addParts(object->m_colorSprite, object);
        if (object->m_glowSprite) addParts(object->m_glowSprite, object);
        paintObject(object);
    }
    for (auto const& sprite : s_sprites) sprite->setColor(kOrange);
    writeState();
    return Ok(s_orange.size());
}

void clearSysLight() {
    auto objects = std::move(s_orange);
    auto sprites = std::move(s_sprites);
    auto colors = std::move(s_colors);
    auto parts = std::move(s_parts);
    s_native.clear();
    s_parts.clear();
    s_drawn.clear();
    s_drawDirty = false;
    s_editor = nullptr;
    s_planned = 0;
    s_excluded = 0;
    s_painted = 0;
    for (std::size_t index = 0; index < sprites.size(); ++index) {
        auto* sprite = sprites[index].data();
        auto owner = parts.find(sprite);
        if (sprite && owner != parts.end() && !owner->second->m_isSelected)
            sprite->setColor(colors[index]);
    }
    for (auto const& object : objects) {
        if (object && !object->m_isSelected) {
            object->updateObjectEditorColor();
        }
    }
    writeState();
}

void recordSysSelect() {
    gd_strip::onLightSelect();
    auto state = gd_strip::lightState();
    if (!state["enabled"].asBool().unwrapOr(false) ||
        state["busy"].asBool().unwrapOr(false)) writeState();
}

std::size_t sysLightCount() {
    return s_orange.size();
}

Result<> loadSysGraph(
    std::string const& sourceHash,
    std::size_t objectCount
) {
    if (sysGraphReady(sourceHash) && s_graph.header().objectCount == objectCount) {
        return Ok();
    }
    auto path = Mod::get()->getSaveDir() / "semantic-graphs" /
        fmt::format("{}.json", lowerASCII(sourceHash));
    GEODE_UNWRAP_INTO(auto root, geode::utils::file::readJson(path));
    if (root["schema"].asString().unwrapOr("") !=
            "gd-strip-causal-graph" ||
        root["schema_major"].asInt().unwrapOr(0) != 1 ||
        lowerASCII(root["source_revision_sha256"].asString().unwrapOr("")) !=
            lowerASCII(sourceHash) ||
        lowerASCII(root["classifier_sha256"].asString().unwrapOr("")) !=
            lowerASCII(std::string(gd_native::kClassHash))) {
        return Err("Causal graph identity is invalid");
    }
    GEODE_UNWRAP_INTO(auto graphText, root["graph_path"].asString());
    GEODE_UNWRAP_INTO(auto graphHash, root["graph_sha256"].asString());
    auto graphPath = std::filesystem::path(graphText);
    GEODE_UNWRAP_INTO(auto actualGraph, geode::sha256File(graphPath));
    if (lowerASCII(actualGraph.toString()) != lowerASCII(graphHash)) {
        return Err("Causal graph digest is invalid");
    }
    gd_graph::Graph graph;
    try {
        graph = gd_graph::Graph::open(graphPath, objectCount);
    }
    catch (std::exception const& error) {
        return Err("{}", error.what());
    }
    auto row = root["roots"];
    GEODE_UNWRAP_INTO(auto seedText, row["path"].asString());
    GEODE_UNWRAP_INTO(auto seedHash, row["sha256"].asString());
    auto seedPath = std::filesystem::path(seedText);
    GEODE_UNWRAP_INTO(auto actualSeed, geode::sha256File(seedPath));
    if (lowerASCII(actualSeed.toString()) != lowerASCII(seedHash)) {
        return Err("Layout root digest is invalid");
    }
    GEODE_UNWRAP_INTO(auto values, readSeeds(seedPath, objectCount));
    s_graph = std::move(graph);
    s_roots = std::move(values);
    s_graphHash = lowerASCII(sourceHash);
    return Ok();
}

Result<std::vector<std::size_t>> querySysGraph(
    std::vector<std::size_t> const& selected
) {
    if (s_graphHash.empty()) return Err("Causal graph is not loaded");
    try {
        auto result = s_graph.querySystem(selected, s_roots);
        return Ok(std::move(result.orange));
    }
    catch (std::exception const& error) {
        return Err("{}", error.what());
    }
}

bool sysGraphReady(std::string const& sourceHash) {
    return !s_graphHash.empty() &&
        s_graphHash == lowerASCII(sourceHash);
}

}

class $modify(StripLightUI, EditorUI) {
    static inline int s_depth = 0;

    void selectObject(GameObject* object, bool ignoreFilter) {
        ++s_depth;
        EditorUI::selectObject(object, ignoreFilter);
        if (--s_depth == 0) gd_strip::recordSysSelect();
    }

    void selectObjects(cocos2d::CCArray* objects, bool ignoreFilter) {
        ++s_depth;
        EditorUI::selectObjects(objects, ignoreFilter);
        if (--s_depth == 0) gd_strip::recordSysSelect();
    }

    void deselectObject(GameObject* object) {
        ++s_depth;
        EditorUI::deselectObject(object);
        if (--s_depth == 0) gd_strip::recordSysSelect();
    }

    void deselectAll() {
        ++s_depth;
        EditorUI::deselectAll();
        if (--s_depth == 0) gd_strip::recordSysSelect();
    }
};

class $modify(StripObjectLight, GameObject) {
    ccColor3B editorColorForMode(int id) {
        return gd_strip::isOrange(this) ? gd_strip::kOrange :
            GameObject::editorColorForMode(id);
    }
    void updateObjectEditorColor() {
        GameObject::updateObjectEditorColor();
        gd_strip::paintObject(this);
    }

    void setObjectColor(cocos2d::ccColor3B const& color) {
        GameObject::setObjectColor(
            gd_strip::isOrange(this) ? gd_strip::kOrange : color
        );
    }
};

class $modify(StripSpriteLight, cocos2d::CCSprite) {
    void setColor(ccColor3B const& color) {
        cocos2d::CCSprite::setColor(
            gd_strip::isOrange(gd_strip::partOwner(this)) ? gd_strip::kOrange : color
        );
    }

    void draw() {
        cocos2d::CCSprite::draw();
        gd_strip::recordDraw(this);
    }
};

class $modify(StripApp, AppDelegate) {
    void platformShutdown() {
        gd_strip::clearPreview();
        AppDelegate::platformShutdown();
    }
};

class $modify(StripLightEdit, LevelEditorLayer) {
    bool init(GJGameLevel* level, bool noUI) {
        if (!LevelEditorLayer::init(level, noUI)) return false;
        if (!noUI) gd_strip::resumeEditor(this);
        return true;
    }

    void updateEditor(float delta) {
        LevelEditorLayer::updateEditor(delta);
        gd_strip::onLightUpdate();
        if (gd_strip::s_drawDirty) {
            gd_strip::s_drawDirty = false;
            gd_strip::writeState();
        }
    }

    void updateObjectSection(GameObject* object) {
        LevelEditorLayer::updateObjectSection(object);
        gd_strip::onLightEdit();
    }

    void removeObject(GameObject* object, bool noUndo) {
        LevelEditorLayer::removeObject(object, noUndo);
        gd_strip::onLightEdit();
    }

};
