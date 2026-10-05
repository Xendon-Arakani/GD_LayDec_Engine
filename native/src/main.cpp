#include <Geode/Geode.hpp>
#include <Geode/modify/EditorPauseLayer.hpp>
#include <Geode/modify/CCKeyboardDispatcher.hpp>
#include <Geode/modify/LocalLevelManager.hpp>
#include <Geode/modify/MenuLayer.hpp>
#include <Geode/utils/cocos.hpp>
#include <Geode/utils/file.hpp>
#include <Geode/utils/hash.hpp>

#include "deco_toggle.hpp"
#include "highlight_system.hpp"

#include <algorithm>
#include <cctype>
#include <optional>
#include <string>
#include <string_view>

using namespace geode::prelude;

namespace {

bool s_launchRan = false;
bool s_levelsReady = false;
bool s_menuReady = false;
bool s_loadStarted = false;

std::string copyText(gd::string const& value) {
    return std::string(value.c_str(), value.size());
}

bool sameName(std::string_view left, std::string_view right) {
    return left.size() == right.size() && std::equal(
        left.begin(), left.end(), right.begin(),
        [](char a, char b) {
            return std::tolower(static_cast<unsigned char>(a)) ==
                std::tolower(static_cast<unsigned char>(b));
        }
    );
}

Result<> openLocal(std::optional<std::string> const& name) {
    auto* levels = LocalLevelManager::sharedState();
    GJGameLevel* found = nullptr;
    std::size_t matches = 0;
    if (levels && levels->m_localLevels) {
        for (auto* level : geode::cocos::CCArrayExt<GJGameLevel*>(
            levels->m_localLevels
        )) {
            if (!level) continue;
            if (!name) {
                found = level;
                break;
            }
            if (sameName(copyText(level->m_levelName), *name)) {
                found = level;
                ++matches;
            }
        }
    }
    if (name && matches > 1) return Err("Local level name is ambiguous: {}", *name);
    if (!found) {
        if (name) return Err("Editable local level not found: {}", *name);
        return Err("No editable local level exists");
    }
    found->m_levelType = GJLevelType::Editor;
    found->m_isEditable = true;
    auto* scene = LevelEditorLayer::scene(found, false);
    if (!scene) return Err("Editor scene creation failed");
    cocos2d::CCDirector::sharedDirector()->replaceScene(scene);
    return Ok();
}

Result<> openStage(std::optional<std::string> const& name) {
    if (!name) return Err("The staged level requires a matching name");
    GEODE_UNWRAP_INTO(
        auto source,
        geode::utils::file::readString(Mod::get()->getSaveDir() / "field.level")
    );
    auto expected = Mod::get()->getLaunchArgument("field-source-hash");
    auto actual = geode::sha256(source).toString();
    if (!expected || !sameName(actual, *expected)) {
        return Err("The staged level hash does not match its launch request");
    }
    auto* game = GameLevelManager::sharedState();
    auto* level = game ? game->createNewLevel() : nullptr;
    if (!level) return Err("Geometry Dash could not create the staged level");
    level->m_levelName = gd::string(*name);
    level->m_levelString = cocos2d::ZipUtils::compressString(
        gd::string(source), false, 0
    );
    level->m_levelType = GJLevelType::Editor;
    level->m_isEditable = true;
    level->m_hasBeenModified = true;
    auto* scene = LevelEditorLayer::scene(level, false);
    if (!scene) return Err("Staged editor scene creation failed");
    cocos2d::CCDirector::sharedDirector()->replaceScene(scene);
    return Ok();
}

Result<> selectFirst() {
    auto* editor = LevelEditorLayer::get();
    auto* ui = EditorUI::get();
    if (!editor || !ui || !editor->getAllObjects()) {
        return Err("Editor selection control is unavailable");
    }
    std::size_t requested = 0;
    if (auto index = Mod::get()->getLaunchArgument("select-index")) {
        try { requested = std::stoull(*index); }
        catch (...) { return Err("Selection index is invalid"); }
    }
    std::size_t index = 0;
    for (auto* object : geode::cocos::CCArrayExt<GameObject*>(
        editor->getAllObjects()
    )) {
        if (!object || object->m_objectID == 749) continue;
        if (index++ != requested) continue;
        ui->selectObject(object, false);
        if (Mod::get()->getLaunchFlag("frame-selected")) {
            ui->centerCameraOnObject(object);
        }
        return Ok();
    }
    return Err("The editor has no selectable level object");
}

void showFail(Result<> const& result);

void runLaunch() {
    if (s_launchRan || !s_menuReady) return;
    auto name = Mod::get()->getLaunchArgument("open-local-name");
    auto latest = Mod::get()->getLaunchFlag("open-latest-local");
    auto strip = Mod::get()->getLaunchFlag("strip-after-open");
    auto audit = Mod::get()->getLaunchFlag("audit-after-open");
    auto select = Mod::get()->getLaunchFlag("select-first");
    auto exit = Mod::get()->getLaunchFlag("exit-on-open");
    auto field = Mod::get()->getLaunchFlag("field-open");
    if (!(name || latest || strip || audit || select || exit || field)) {
        return;
    }
    if (field) s_levelsReady = true;
    if (!s_levelsReady) return;
    s_launchRan = true;
    geode::queueInMainThread([name, strip, audit, select, exit, field] {
        auto opened = field ? openStage(name) : openLocal(name);
        if (!opened) {
            showFail(opened);
            return;
        }
        geode::queueInMainThread([strip, audit, select, exit] {
            if (strip || audit) {
                gd_strip::clearSysLight();
                showFail(gd_strip::extractLayout(audit));
            }
            if (select) showFail(selectFirst());
            if (exit) {
                auto* editor = LevelEditorLayer::get();
                auto* pause = editor
                    ? EditorPauseLayer::create(editor) : nullptr;
                if (!pause) {
                    showFail(Err("Editor exit control could not be created"));
                    return;
                }
                pause->onExitEditor(nullptr);
            }
        });
    });
}

void showFail(Result<> const& result) {
    if (result) return;
    log::error("{}", result.unwrapErr());
}

class $modify(StripPause, EditorPauseLayer) {
    bool init(LevelEditorLayer* layer) {
        if (!EditorPauseLayer::init(layer)) return false;
        gd_strip::addExtractMenu(this);
        return true;
    }

    void saveLevel() {
        auto* editor = LevelEditorLayer::get();
        auto hadUndo = editor && editor->m_undoObjects &&
            editor->m_undoObjects->count() > 0;
        EditorPauseLayer::saveLevel();
        showFail(gd_strip::recordStripSave(hadUndo));
    }

    void onExitEditor(cocos2d::CCObject* sender) {
        auto* editor = LevelEditorLayer::get();
        if (editor) {
            editor->unscheduleUpdate();
            editor->unscheduleAllSelectors();
        }
        gd_strip::clearPreview();
        EditorPauseLayer::onExitEditor(sender);
        gd_strip::leaveToggle(true);
    }

    void onExitNoSave(cocos2d::CCObject* sender) {
        auto* editor = LevelEditorLayer::get();
        if (editor) {
            editor->unscheduleUpdate();
            editor->unscheduleAllSelectors();
        }
        gd_strip::clearPreview();
        gd_strip::leaveToggle(false);
        EditorPauseLayer::onExitNoSave(sender);
    }
};

class $modify(StripMenu, MenuLayer) {
    bool init() {
        if (!MenuLayer::init()) return false;
        auto* levels = LocalLevelManager::sharedState();
        s_menuReady = true;
        auto field = Mod::get()->getLaunchFlag("field-open");
        if (field) {
            s_levelsReady = true;
        } else if (levels && (!levels->m_localLevels ||
            levels->m_localLevels->count() == 0) && !s_loadStarted) {
            s_loadStarted = true;
            levels->load();
        } else if (levels && levels->m_localLevels) {
            s_levelsReady = true;
        }
        runLaunch();
        return true;
    }
};

class $modify(StripLevels, LocalLevelManager) {
    void dataLoaded(DS_Dictionary* data) {
        LocalLevelManager::dataLoaded(data);
        s_levelsReady = true;
        geode::queueInMainThread([] { runLaunch(); });
    }
};

class $modify(StripKeys, cocos2d::CCKeyboardDispatcher) {
    bool dispatchKeyboardMSG(
        cocos2d::enumKeyCodes key,
        bool down,
        bool repeat,
        double time
    ) {
        if (gd_strip::blocksInput() && down) return true;
        return cocos2d::CCKeyboardDispatcher::dispatchKeyboardMSG(
            key, down, repeat, time
        );
    }
};


}
