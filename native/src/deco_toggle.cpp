#include "deco_toggle.hpp"
#include "highlight_system.hpp"
#include "prior_style.hpp"

#include <Geode/loader/Dirs.hpp>
#include <Geode/utils/cocos.hpp>
#include <Geode/utils/file.hpp>
#include <Geode/utils/hash.hpp>
#include <Geode/ui/Popup.hpp>

#include <native_sem.hpp>

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <map>
#include <optional>
#include <ranges>
#include <set>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <vector>

#ifdef GEODE_IS_WINDOWS
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

using namespace geode::prelude;

namespace gd_strip {
namespace {

constexpr std::string_view kBindingsCommit =
    "7f6c2a75742856de88dad354e576dcff8a28e881";
constexpr std::int64_t kPlanSchemaMinor = 8;

std::string copyString(gd::string const& value) {
    return std::string(value.c_str(), value.size());
}

gd::string encodeLevelText(std::string_view exact) {
    return cocos2d::ZipUtils::compressString(
        gd::string(exact.data(), exact.size()), false, 0
    );
}

std::string decodeLevelText(gd::string const& stored) {
    return copyString(cocos2d::ZipUtils::decompressString(stored, false, 0));
}

std::vector<std::string> splitPreservingEmpty(
    std::string_view value,
    char delimiter
) {
    std::vector<std::string> parts;
    std::size_t start = 0;
    while (start <= value.size()) {
        auto end = value.find(delimiter, start);
        if (end == std::string_view::npos) {
            parts.emplace_back(value.substr(start));
            break;
        }
        parts.emplace_back(value.substr(start, end - start));
        start = end + 1;
    }
    return parts;
}

struct StripLoadMatch {
    std::size_t mainColorFixes = 0;
    std::size_t detailColorFixes = 0;
    std::size_t groupRenumbers = 0;
    std::size_t keyPathRenumbers = 0;
    std::size_t editorLayerFixes = 0;
};

Result<std::vector<std::pair<std::string, std::string>>> objectPairs(
    std::string_view record
) {
    auto tokens = splitPreservingEmpty(record, ',');
    if (tokens.size() % 2 != 0) {
        return Err("Object record has an incomplete key/value pair");
    }
    std::vector<std::pair<std::string, std::string>> pairs;
    pairs.reserve(tokens.size() / 2);
    for (std::size_t index = 0; index < tokens.size(); index += 2) {
        pairs.emplace_back(tokens[index], tokens[index + 1]);
    }
    return Ok(std::move(pairs));
}

Result<StripLoadMatch> verifyFreshStrip(
    std::string_view expected,
    std::string_view actual
) {
    auto expectedSegments = splitPreservingEmpty(expected, ';');
    auto actualSegments = splitPreservingEmpty(actual, ';');
    if (expectedSegments.size() != actualSegments.size()) {
        return Err("Fresh editor changed the retained object count");
    }
    if (expectedSegments.empty() || expectedSegments.front() != actualSegments.front()) {
        return Err("Fresh editor changed level settings during stripping");
    }
    StripLoadMatch summary;
    std::map<std::string, std::string> expToActGroups;
    std::map<std::string, std::string> actToExpGroups;
    std::map<std::string, std::string> expToActKeyPaths;
    std::map<std::string, std::string> actToExpKeyPaths;
    for (std::size_t segment = 1; segment < expectedSegments.size(); ++segment) {
        if (expectedSegments[segment].empty() && actualSegments[segment].empty()) {
            continue;
        }
        GEODE_UNWRAP_INTO(auto expectedPairs, objectPairs(expectedSegments[segment]));
        GEODE_UNWRAP_INTO(auto actualPairs, objectPairs(actualSegments[segment]));
        auto valueFor = [](auto const& pairs, std::string_view key) {
            std::optional<std::string> result;
            for (auto const& pair : pairs) {
                if (pair.first == key) result = pair.second;
            }
            return result;
        };
        auto const expectedLinkedGroup = valueFor(expectedPairs, "108");
        auto const actualLinkedGroup = valueFor(actualPairs, "108");
        if (expectedLinkedGroup.has_value() != actualLinkedGroup.has_value()) {
            return Err(
                "Fresh editor added or removed a linked-group relation at retained segment {}: expected [{}], loaded [{}]",
                segment, expectedSegments[segment], actualSegments[segment]
            );
        }
        if (expectedLinkedGroup && actualLinkedGroup) {
            auto [expectedIt, insertedExpected] = expToActGroups.emplace(
                *expectedLinkedGroup, *actualLinkedGroup
            );
            if (!insertedExpected && expectedIt->second != *actualLinkedGroup) {
                return Err(
                    "Fresh editor split linked group {} between loaded groups {} and {} at retained segment {}",
                    *expectedLinkedGroup, expectedIt->second, *actualLinkedGroup, segment
                );
            }
            auto [actualIt, insertedActual] = actToExpGroups.emplace(
                *actualLinkedGroup, *expectedLinkedGroup
            );
            if (!insertedActual && actualIt->second != *expectedLinkedGroup) {
                return Err(
                    "Fresh editor merged linked groups {} and {} into loaded group {} at retained segment {}",
                    actualIt->second, *expectedLinkedGroup, *actualLinkedGroup, segment
                );
            }
            if (*expectedLinkedGroup != *actualLinkedGroup) {
                ++summary.groupRenumbers;
            }
        }
        auto const expectedObjectID = valueFor(expectedPairs, "1");
        auto const actualObjectID = valueFor(actualPairs, "1");
        auto layerSet = [&valueFor](auto const& pairs) {
            std::set<std::string> result;
            for (auto key : {"20", "61"}) {
                auto value = valueFor(pairs, key);
                if (value && *value != "0") result.insert(*value);
            }
            return result;
        };
        auto expectedLayers = layerSet(expectedPairs);
        auto actualLayers = layerSet(actualPairs);
        if (expectedLayers != actualLayers) {
            return Err(
                "Fresh editor changed editor-layer membership at retained segment {}: expected [{}], loaded [{}]",
                segment, expectedSegments[segment], actualSegments[segment]
            );
        }
        if (valueFor(expectedPairs, "20") != valueFor(actualPairs, "20") ||
            valueFor(expectedPairs, "61") != valueFor(actualPairs, "61")) {
            ++summary.editorLayerFixes;
        }
        auto const isKeyframePoint =
            expectedObjectID == std::optional<std::string>("3032") &&
            actualObjectID == expectedObjectID;
        if (isKeyframePoint) {
            auto const expectedPath = valueFor(expectedPairs, "373").value_or("0");
            auto const actualPath = valueFor(actualPairs, "373").value_or("0");
            auto [expectedIt, insertedExpected] = expToActKeyPaths.emplace(
                expectedPath, actualPath
            );
            if (!insertedExpected && expectedIt->second != actualPath) {
                return Err(
                    "Fresh editor split keyframe path {} between loaded paths {} and {} at retained segment {}",
                    expectedPath, expectedIt->second, actualPath, segment
                );
            }
            auto [actualIt, insertedActual] = actToExpKeyPaths.emplace(
                actualPath, expectedPath
            );
            if (!insertedActual && actualIt->second != expectedPath) {
                return Err(
                    "Fresh editor merged keyframe paths {} and {} into loaded path {} at retained segment {}",
                    actualIt->second, expectedPath, actualPath, segment
                );
            }
            if (expectedPath != actualPath) {
                ++summary.keyPathRenumbers;
            }
        }
        auto filtered = [isKeyframePoint](auto const& pairs) {
            std::vector<std::pair<std::string, std::string>> result;
            for (auto const& pair : pairs) {

                if (pair.first == "20" || pair.first == "61" ||
                    pair.first == "108" || pair.first == "155" ||
                    pair.first == "156" ||
                    (isKeyframePoint && pair.first == "373")) continue;
                result.push_back(pair);
            }
            return result;
        };
        if (filtered(expectedPairs) != filtered(actualPairs)) {
            return Err(
                "Fresh editor changed a non-color-cache property at retained segment {}: expected [{}], loaded [{}]",
                segment, expectedSegments[segment], actualSegments[segment]
            );
        }
        if (valueFor(expectedPairs, "155") != valueFor(actualPairs, "155")) {
            ++summary.mainColorFixes;
        }
        if (valueFor(expectedPairs, "156") != valueFor(actualPairs, "156")) {
            ++summary.detailColorFixes;
        }
    }
    return Ok(summary);
}

struct PlannedObjectRecord {
    std::size_t serializationIndex = 0;
    int objectID = 0;
    std::string exactSaveString;
};

struct LoadedRemovalPlan {
    std::filesystem::path path;
    std::string levelName;
    std::string sourceLevelString;
    std::string sourceHash;
    std::string expectedStrippedHash;
    std::string planHash;
    std::vector<PlannedObjectRecord> fullRecords;
    std::vector<PlannedObjectRecord> removedRecords;
};

struct ToggleSession {
    LoadedRemovalPlan plan;
    std::filesystem::path recoveryPath;
    std::string strippedLevelString;
    std::string strippedHash;
    LevelEditorLayer* editor = nullptr;
    GJGameLevel* level = nullptr;
    bool strippedStateSaved = false;
};

std::optional<ToggleSession> s_session;
bool s_lightOn = false;
bool s_lightBusy = false;
bool s_lightRefreshPending = false;
std::optional<LoadedRemovalPlan> s_lightPlan;
LevelEditorLayer* s_lightEditor = nullptr;
std::string s_lightHash;
std::string s_lightError;
std::function<void()> s_lightSync;
std::vector<GameObject*> s_lightByIndex;
std::unordered_map<GameObject*, std::size_t> s_lightIds;
cocos2d::CCObject* s_lightUndo = nullptr;
cocos2d::CCObject* s_lightRedo = nullptr;
unsigned int s_lightObjects = 0;
unsigned int s_lightUndos = 0;
unsigned int s_lightRedos = 0;
bool s_lightMapped = false;
std::string s_lightMapPlan;
bool s_busy = false;
geode::Ref<Notification> s_toggleNotice;
std::string s_toggleOperationID;
std::string s_toggleAction;
std::string s_toggleLevelName;
std::string s_toggleSourceHash;
std::size_t s_toggleObjectCount = 0;
std::string s_toggleStage;
std::string s_toggleResultHash;
std::string s_toggleMessage;
bool s_toggleTerminal = false;
bool s_toggleSuccess = false;
std::int64_t s_capStarted = 0;
std::int64_t s_capFinished = 0;
std::int64_t s_mapStarted = 0;
std::int64_t s_applyStarted = 0;
bool s_capLocked = false;
bool s_mapUnlocked = false;
bool s_openSaved = false;

enum class FlowPhase {
    Idle,
    Capture,
    Mapping,
    Switching,
    Applying,
};

struct FlowMark {
    std::string event;
    std::string from;
    std::string to;
    std::int64_t time = 0;
    bool inputLocked = false;
    bool valid = false;
};

class InputShield final : public cocos2d::CCLayer {
public:
    static InputShield* create() {
        auto* shield = new InputShield();
        if (!shield || !shield->init()) {
            CC_SAFE_DELETE(shield);
            return nullptr;
        }
        shield->autorelease();
        return shield;
    }

    bool init() override {
        if (!cocos2d::CCLayer::init()) return false;
        setTouchMode(cocos2d::kCCTouchesOneByOne);
        setTouchPriority(std::numeric_limits<int>::min() + 1);
        setTouchEnabled(true);
        return true;
    }

    bool ccTouchBegan(
        cocos2d::CCTouch*, cocos2d::CCEvent*
    ) override {
        return true;
    }
};

FlowPhase s_flowPhase = FlowPhase::Idle;
std::vector<FlowMark> s_flowMarks;
std::size_t s_flowRejects = 0;
geode::Ref<InputShield> s_inputShield;
std::optional<std::array<bool, 4>> s_editInputBefore;
bool s_inputHeld = false;

void showToggleNotice(
    std::string const& message,
    NotificationIcon icon,
    float duration
) {
    if (s_toggleNotice && s_toggleNotice->isShowing()) {
        s_toggleNotice->stopAllActions();
        s_toggleNotice->setString(message);
        s_toggleNotice->setIcon(icon);
        s_toggleNotice->setTime(0.0f);
        if (duration > 0.0f) {
            auto retained = s_toggleNotice;
            s_toggleNotice->runAction(cocos2d::CCSequence::create(
                cocos2d::CCDelayTime::create(duration),
                geode::cocos::CallFuncExt::create([retained] {
                    if (retained && retained->isShowing()) retained->hide();
                }), nullptr
            ));
        }
        return;
    }
    if (s_toggleNotice) s_toggleNotice->cancel();
    auto* notice = Notification::create(message, icon, duration);
    s_toggleNotice = geode::Ref<Notification>(notice);
    notice->show();
}

void dropInputGate() {
    if (s_inputShield) {
        s_inputShield->removeFromParentAndCleanup(true);
        s_inputShield = nullptr;
    }
    s_editInputBefore.reset();
    s_inputHeld = false;
}

void freeInputGate() {
    dropInputGate();
}

Result<> holdInputGate(
    LevelEditorLayer* editor,
    cocos2d::CCNode* root = nullptr
) {
    if (s_inputHeld || s_inputShield) {
        return Err("An input gate is already active");
    }
    if (!root) root = cocos2d::CCDirector::sharedDirector()->getRunningScene();
    if (!root) return Err("The active screen is unavailable for input gating");
    if (editor && editor->m_editorUI) {
        s_editInputBefore = std::array{
            editor->isTouchEnabled(), editor->isKeypadEnabled(),
            editor->m_editorUI->isTouchEnabled(),
            editor->m_editorUI->isKeypadEnabled()
        };
    } else {
        s_editInputBefore.reset();
    }
    auto* shield = InputShield::create();
    if (!shield) return Err("The active screen input gate could not be created");
    root->addChild(shield, std::numeric_limits<int>::max());
    s_inputShield = geode::Ref<InputShield>(shield);
    s_inputHeld = true;
    return Ok();
}

std::int64_t unixMilliseconds() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()
    ).count();
}

std::string_view flowName(FlowPhase phase) {
    switch (phase) {
        case FlowPhase::Idle: return "idle";
        case FlowPhase::Capture: return "capture";
        case FlowPhase::Mapping: return "mapping";
        case FlowPhase::Switching: return "switching";
        case FlowPhase::Applying: return "applying";
    }
    return "invalid";
}

bool inputLocked(LevelEditorLayer* editor) {
    return s_inputHeld && s_inputShield &&
        s_inputShield->getParent() && s_inputShield->isTouchEnabled();
}

bool editInputSame(LevelEditorLayer* editor) {
    if (!s_editInputBefore) return !editor;
    if (!editor || !editor->m_editorUI) return false;
    return *s_editInputBefore == std::array{
        editor->isTouchEnabled(), editor->isKeypadEnabled(),
        editor->m_editorUI->isTouchEnabled(),
        editor->m_editorUI->isKeypadEnabled()
    };
}

void writeFlowMap() {
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-flow-gates";
    root["operation_id"] = s_toggleOperationID;
    root["phase"] = std::string(flowName(s_flowPhase));
    root["busy"] = s_busy;
    root["overlap_rejections"] = static_cast<std::int64_t>(s_flowRejects);
    root["events"] = matjson::Value::array();
    for (auto const& mark : s_flowMarks) {
        auto row = matjson::Value::object();
        row["event"] = mark.event;
        row["from"] = mark.from;
        row["to"] = mark.to;
        row["unix_ms"] = mark.time;
        row["input_locked"] = mark.inputLocked;
        row["gate_verified"] = mark.valid;
        root["events"].push(std::move(row));
    }
    auto path = Mod::get()->getSaveDir() / "toggle-audits" /
        "flow-latest.json";
    std::error_code error;
    std::filesystem::create_directories(path.parent_path(), error);
    if (error) {
        log::error("Flow gate directory failed: {}", error.message());
        return;
    }
    auto written = geode::utils::file::writeStringSafe(path, root.dump());
    if (!written) log::error("Flow gate state failed: {}", written.unwrapErr());
}

void markFlow(
    std::string_view event,
    FlowPhase from,
    FlowPhase to,
    LevelEditorLayer* editor,
    bool valid
) {
    s_flowMarks.push_back(FlowMark{
        std::string(event), std::string(flowName(from)),
        std::string(flowName(to)), unixMilliseconds(),
        inputLocked(editor), valid
    });
    writeFlowMap();
}

Result<> startFlow(FlowPhase phase, LevelEditorLayer* editor) {
    if (s_busy || s_flowPhase != FlowPhase::Idle) {
        ++s_flowRejects;
        markFlow("overlap_rejected", s_flowPhase, phase, editor, false);
        return Err("A level operation is already working");
    }
    s_flowMarks.clear();
    s_flowRejects = 0;
    auto prior = s_flowPhase;
    s_flowPhase = phase;
    s_busy = true;
    markFlow("operation_started", prior, phase, editor, true);
    return Ok();
}

Result<> verifyFlow(
    FlowPhase phase,
    LevelEditorLayer* editor,
    bool locked,
    std::string_view event
) {
    auto valid = s_busy && s_flowPhase == phase &&
        inputLocked(editor) == locked;
    markFlow(event, s_flowPhase, s_flowPhase, editor, valid);
    if (!valid) return Err("The extraction input gate is inconsistent");
    return Ok();
}

Result<> moveFlow(
    FlowPhase expected,
    FlowPhase next,
    LevelEditorLayer* editor,
    bool locked,
    std::string_view event
) {
    auto valid = s_busy && s_flowPhase == expected &&
        inputLocked(editor) == locked;
    auto prior = s_flowPhase;
    if (valid) s_flowPhase = next;
    markFlow(event, prior, valid ? next : prior, editor, valid);
    if (!valid) return Err("The extraction phase gate is inconsistent");
    return Ok();
}

void stopFlow(LevelEditorLayer* editor, std::string_view event) {
    freeInputGate();
    auto prior = s_flowPhase;
    s_flowPhase = FlowPhase::Idle;
    s_busy = false;
    markFlow(event, prior, FlowPhase::Idle, editor, true);
}

struct ExactCopy {
    geode::Ref<GJGameLevel> level;
    std::string name;
    std::string source;
    std::string sourceHash;
};

Result<ExactCopy> createExactCopy(
    LevelEditorLayer* editor,
    std::string_view purpose,
    std::string_view toolLabel = "",
    std::optional<std::string_view> sourceText = std::nullopt
) {
    if (!editor || !editor->m_level) {
        return Err("The active level is unavailable for copying");
    }
    auto* local = LocalLevelManager::sharedState();
    auto* game = GameLevelManager::sharedState();
    if (!local || !local->m_localLevels || !game) {
        return Err("Geometry Dash local-level storage is unavailable");
    }
    auto exact = sourceText
        ? std::string(*sourceText) : copyString(editor->getLevelString());
    if (exact.empty()) return Err("The active editor revision is empty");
    auto const exactHash = geode::sha256(exact).toString();

    auto base = toolLabel.empty()
        ? fmt::format("{} [{}]", copyString(editor->m_level->m_levelName), purpose)
        : fmt::format(
            "{} [{} {}]", copyString(editor->m_level->m_levelName), toolLabel,
            purpose
        );
    auto nameExists = [local](std::string_view name) {
        for (auto* level : geode::cocos::CCArrayExt<GJGameLevel*>(
                 local->m_localLevels
             )) {
            if (level && copyString(level->m_levelName) == name) return true;
        }
        return false;
    };
    auto uniqueName = base;
    for (std::size_t suffix = 2; nameExists(uniqueName); ++suffix) {
        uniqueName = fmt::format("{} {}", base, suffix);
    }

    auto* backup = game->createNewLevel();
    if (!backup) return Err("Geometry Dash could not create the level copy");
    backup->copyLevelInfo(editor->m_level);
    backup->m_levelName = gd::string(uniqueName);
    backup->m_levelString = encodeLevelText(exact);
    backup->m_levelType = GJLevelType::Editor;
    backup->m_isEditable = true;
    backup->m_hasBeenModified = true;
    editor->m_level->m_levelString = encodeLevelText(exact);
    editor->m_level->m_hasBeenModified = true;
    local->save();

    bool registered = false;
    for (auto* candidate : geode::cocos::CCArrayExt<GJGameLevel*>(
             local->m_localLevels
         )) {
        if (candidate == backup) {
            registered = true;
            break;
        }
    }
    auto persisted = decodeLevelText(backup->m_levelString);
    auto sourceSaved = decodeLevelText(editor->m_level->m_levelString);
    if (!registered || copyString(backup->m_levelName) != uniqueName ||
        persisted != exact || sourceSaved != exact ||
        geode::sha256(persisted).toString() != exactHash) {
        return Err("Geometry Dash did not retain the level copy in memory");
    }
    log::info(
        "Created level copy {} at revision {}",
        uniqueName, exactHash
    );
    return Ok(ExactCopy{
        geode::Ref<GJGameLevel>(backup), std::move(uniqueName),
        std::move(exact), std::move(exactHash)
    });
}

std::filesystem::path recoveryDirectory() {
    return Mod::get()->getSaveDir() / "toggle-recovery" / "alternates";
}

std::filesystem::path oldRecoveryPath() {
    return Mod::get()->getSaveDir() / "toggle-recovery" / "active.json";
}

std::filesystem::path recoveryJournalPath(
    LoadedRemovalPlan const& plan,
    GJGameLevel* level
) {
    auto identity = fmt::format(
        "{}\n{}\n{}\n{}\n{}",
        plan.levelName,
        plan.sourceHash,
        level ? level->m_levelID.value() : 0,
        level ? level->m_originalLevel.value() : 0,
        level ? level->m_levelIndex : -1
    );
    return recoveryDirectory() / fmt::format(
        "{}.json", geode::sha256(identity).toString()
    );
}

Result<> writeRecoveryJournal(
    std::filesystem::path const& path,
    LoadedRemovalPlan const& plan,
    GJGameLevel* level,
    std::optional<std::string_view> liveStrippedHash = std::nullopt,
    std::optional<std::string_view> savedLevelStringHash = std::nullopt,
    std::string_view state = "stripped"
) {
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-decoration-toggle-recovery";
    root["schema_major"] = 1;
    root["game_version"] = Loader::get()->getGameVersion();
    root["bridge_version"] = Mod::get()->getVersion().toNonVString();
    root["level_name"] = plan.levelName;
    root["level_id"] = static_cast<std::int64_t>(
        level ? level->m_levelID.value() : 0
    );
    root["original_level_id"] = static_cast<std::int64_t>(
        level ? level->m_originalLevel.value() : 0
    );
    root["level_index"] = static_cast<std::int64_t>(
        level ? level->m_levelIndex : -1
    );
    root["source_revision_sha256"] = plan.sourceHash;
    root["static_filtered_revision_sha256"] = plan.expectedStrippedHash;
    root["plan_sha256"] = plan.planHash;
    root["removed_object_count"] = static_cast<std::int64_t>(
        plan.removedRecords.size()
    );
    root["written_unix_ms"] = unixMilliseconds();
    root["state"] = liveStrippedHash ? std::string(state) : "mutation_pending";
    root["live_stripped_revision_sha256"] = liveStrippedHash
        ? matjson::Value(std::string(*liveStrippedHash)) : matjson::Value(nullptr);
    root["saved_level_string_sha256"] = savedLevelStringHash
        ? matjson::Value(std::string(*savedLevelStringHash)) : matjson::Value(nullptr);
    root["source_level_string"] = plan.sourceLevelString;
    std::error_code error;
    std::filesystem::create_directories(path.parent_path(), error);
    if (error) return Err(error.message());
    return geode::utils::file::writeStringSafe(path, root.dump());
}

void clearRecoveryJournal(std::filesystem::path const& path) {
    std::error_code error;
    std::filesystem::remove(path, error);
    if (error) log::error("Could not clear decoration recovery journal: {}", error.message());
}

void writeToggleProgress(
    std::string_view stage,
    std::string_view levelName,
    std::string_view sourceHash,
    std::size_t objectCount,
    std::optional<std::string_view> resultHash = std::nullopt,
    std::optional<std::string_view> message = std::nullopt
) {
    if (s_toggleOperationID.empty()) {
        auto const now = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()
        ).count();
        s_toggleOperationID = fmt::format(
            "{}-{}", now, std::string(sourceHash).substr(0, 12)
        );
        s_toggleLevelName = levelName;
        s_toggleSourceHash = sourceHash;
        s_toggleObjectCount = objectCount;
    }
    s_toggleAction = "extract";
    auto const terminalSuccess = stage == "removed_exact";
    auto const terminalFailure = stage.ends_with("_failed");
    s_toggleStage = stage;
    if (stage == "removed_exact") {
        s_toggleObjectCount = objectCount;
    }
    s_toggleResultHash = resultHash ? std::string(*resultHash) : std::string{};
    s_toggleMessage = message ? std::string(*message) : std::string{};
    s_toggleTerminal = terminalSuccess || terminalFailure;
    s_toggleSuccess = terminalSuccess;
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-decoration-toggle-status";
    root["schema_major"] = 1;
    root["operation_id"] = s_toggleOperationID;
    root["bridge_version"] = Mod::get()->getVersion().toNonVString();
    root["action"] = s_toggleAction;
    root["phase"] = std::string(stage);
    root["status"] = terminalSuccess ? "completed" :
        terminalFailure ? "failed" : "working";
    root["terminal"] = terminalSuccess || terminalFailure;
    root["success"] = terminalSuccess ? matjson::Value(true) :
        terminalFailure ? matjson::Value(false) : matjson::Value(nullptr);
    root["level_name"] = s_toggleLevelName;
    root["source_revision_sha256"] = s_toggleSourceHash;
    root["result_revision_sha256"] = resultHash
        ? matjson::Value(std::string(*resultHash)) : matjson::Value(nullptr);
    root["removed_object_count"] =
        static_cast<std::int64_t>(s_toggleObjectCount);
    root["updated_unix_ms"] = static_cast<std::int64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()
        ).count()
    );
    root["message"] = message
        ? matjson::Value(std::string(*message)) : matjson::Value(nullptr);
    auto directory = Mod::get()->getSaveDir() / "toggle-audits";
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    if (!error) {
        auto result = geode::utils::file::writeStringSafe(
            directory / "status-latest.json", root.dump()
        );
        if (!result) log::error("Toggle progress write failed: {}", result.unwrapErr());
        auto compatibility = geode::utils::file::writeStringSafe(
            directory / "progress.json", root.dump()
        );
        if (!compatibility) {
            log::error("Toggle compatibility-status write failed: {}", compatibility.unwrapErr());
        }
    }
}

void writeToggleFailure(std::string_view message) {
    if (s_toggleOperationID.empty()) return;
    writeToggleProgress(
        "operation_failed", s_toggleLevelName, s_toggleSourceHash,
        s_toggleObjectCount, std::nullopt, message
    );
}

Result<std::string> requiredText(
    matjson::Value const& value,
    std::string_view key
) {
    if (!value.contains(key)) return Err("Removal plan lacks {}", key);
    auto result = value[key].asString();
    if (!result) return Err("Removal plan {} is not text", key);
    return Ok(result.unwrap());
}

Result<std::int64_t> requiredInteger(
    matjson::Value const& value,
    std::string_view key
) {
    if (!value.contains(key)) return Err("Removal plan lacks {}", key);
    auto result = value[key].asInt();
    if (!result) return Err("Removal plan {} is not an integer", key);
    return Ok(result.unwrap());
}

Result<> requireTrue(matjson::Value const& value, std::string_view key) {
    if (!value.contains(key)) return Err("Removal plan lacks {}", key);
    auto result = value[key].asBool();
    if (!result || !result.unwrap()) {
        return Err("Removal plan is not eligible: {}", key);
    }
    return Ok();
}

std::string lowercaseASCII(std::string value) {
    std::transform(
        value.begin(), value.end(), value.begin(),
        [](unsigned char character) {
            return static_cast<char>(std::tolower(character));
        }
    );
    return value;
}

Result<std::string> classifierHash() {
    return Ok(std::string(gd_native::kClassHash));
}

struct StoredAlt {
    std::filesystem::path path;
    std::string state;
    std::string levelName;
    std::int64_t levelID = 0;
    std::int64_t originalLevelID = 0;
    std::int64_t levelIndex = -1;
    std::string sourceHash;
    std::string liveStrippedHash;
    std::string savedLevelStringHash;
    std::string expectedStrippedHash;
    std::string planHash;
    std::size_t removedObjectCount = 0;
    std::int64_t writtenAt = 0;
    bool hasWrittenAt = false;
    std::string sourceLevelString;
};

Result<StoredAlt> readStoredAlt(
    std::filesystem::path const& path
) {
    GEODE_UNWRAP_INTO(auto serialized, geode::utils::file::readString(path));
    GEODE_UNWRAP_INTO(auto root, matjson::parse(serialized));
    GEODE_UNWRAP_INTO(auto schema, requiredText(root, "schema"));
    GEODE_UNWRAP_INTO(auto state, requiredText(root, "state"));
    GEODE_UNWRAP_INTO(auto levelName, requiredText(root, "level_name"));
    GEODE_UNWRAP_INTO(auto sourceHash, requiredText(root, "source_revision_sha256"));
    GEODE_UNWRAP_INTO(auto sourceLevelString, requiredText(root, "source_level_string"));
    if (schema != "gd-strip-decoration-toggle-recovery") {
        return Err("Unknown decoration alternate-state schema");
    }
    std::string liveStrippedHash;
    if (root.contains("live_stripped_revision_sha256")) {
        auto live = root["live_stripped_revision_sha256"].asString();
        if (live) liveStrippedHash = live.unwrap();
    }
    std::string savedLevelStringHash;
    if (root.contains("saved_level_string_sha256")) {
        auto saved = root["saved_level_string_sha256"].asString();
        if (saved) savedLevelStringHash = saved.unwrap();
    }
    std::string expectedStrippedHash;
    if (root.contains("static_filtered_revision_sha256")) {
        auto expected = root["static_filtered_revision_sha256"].asString();
        if (expected) expectedStrippedHash = expected.unwrap();
    }
    std::string planHash;
    if (root.contains("plan_sha256")) {
        auto plan = root["plan_sha256"].asString();
        if (plan) planHash = plan.unwrap();
    }
    auto optionalInteger = [&root](std::string_view key, std::int64_t fallback) {
        if (!root.contains(key)) return fallback;
        auto value = root[key].asInt();
        return value ? value.unwrap() : fallback;
    };
    auto writtenAt = optionalInteger("written_unix_ms", 0);
    auto hasWrittenAt = writtenAt > 0;
    if (!hasWrittenAt) {
        std::error_code error;
        auto modified = std::filesystem::last_write_time(path, error);
        if (!error) writtenAt = modified.time_since_epoch().count();
    }
    return Ok(StoredAlt{
        path, std::move(state), std::move(levelName),
        optionalInteger("level_id", 0),
        optionalInteger("original_level_id", 0),
        optionalInteger("level_index", -1),
        std::move(sourceHash),
        std::move(liveStrippedHash), std::move(savedLevelStringHash),
        std::move(expectedStrippedHash), std::move(planHash),
        static_cast<std::size_t>(std::max<std::int64_t>(
            optionalInteger("removed_object_count", 0), 0
        )),
        writtenAt, hasWrittenAt,
        std::move(sourceLevelString)
    });
}

Result<std::vector<StoredAlt>> readStoredAlts() {
    std::vector<StoredAlt> alternates;
    auto readIfPresent = [&alternates](std::filesystem::path const& path) -> Result<> {
        if (!std::filesystem::exists(path)) return Ok();
        GEODE_UNWRAP_INTO(auto alternate, readStoredAlt(path));
        alternates.push_back(std::move(alternate));
        return Ok();
    };
    GEODE_UNWRAP(readIfPresent(oldRecoveryPath()));

    std::error_code error;
    if (!std::filesystem::exists(recoveryDirectory(), error)) {
        if (error) return Err(error.message());
        return Ok(std::move(alternates));
    }
    for (auto const& entry : std::filesystem::directory_iterator(
             recoveryDirectory(), error
         )) {
        if (error) return Err(error.message());
        if (!entry.is_regular_file() || entry.path().extension() != ".json") continue;
        GEODE_UNWRAP(readIfPresent(entry.path()));
    }
    return Ok(std::move(alternates));
}

Result<std::vector<PlannedObjectRecord>> parseRecords(
    matjson::Value const& root,
    std::string_view key,
    std::size_t sourceCount
) {
    if (!root.contains(key)) return Err("Removal plan lacks {}", key);
    auto arrayResult = root[key].asArray();
    if (!arrayResult) return Err("Removal plan {} is not an array", key);
    std::vector<PlannedObjectRecord> records;
    for (auto const& row : arrayResult.unwrap()) {
        auto indexResult = requiredInteger(row, "serialization_order_index");
        auto objectIDResult = requiredInteger(row, "object_id");
        auto exactResult = requiredText(row, "exact_save_string");
        auto hashResult = requiredText(row, "exact_save_string_sha256");
        if (!indexResult || !objectIDResult || !exactResult || !hashResult) {
            return Err("Removal plan {} contains a malformed record", key);
        }
        auto const rawIndex = indexResult.unwrap();
        auto const rawObjectID = objectIDResult.unwrap();
        if (rawIndex < 0 || static_cast<std::size_t>(rawIndex) >= sourceCount ||
            rawObjectID <= 0) {
            return Err("Removal plan {} has an invalid record identity", key);
        }
        auto exact = exactResult.unwrap();
        if (lowercaseASCII(geode::sha256(exact).toString()) !=
            lowercaseASCII(hashResult.unwrap())) {
            return Err("Removal plan record {} failed its raw hash", rawIndex);
        }
        records.push_back({
            static_cast<std::size_t>(rawIndex),
            static_cast<int>(rawObjectID),
            std::move(exact),
        });
    }
    return Ok(std::move(records));
}

Result<LoadedRemovalPlan> loadPlanFile(
    std::filesystem::path const& path,
    std::string source
) {
    auto sourceHash = geode::sha256(source).toString();
    auto fileResult = geode::utils::file::readString(path);
    if (!fileResult) {
        return Err("No verified removal plan exists for this editor revision");
    }
    auto parseResult = matjson::parse(fileResult.unwrap());
    if (!parseResult) return Err("Removal plan JSON could not be parsed");
    auto root = parseResult.unwrap();

    auto schema = requiredText(root, "schema");
    auto major = requiredInteger(root, "schema_major");
    auto minor = requiredInteger(root, "schema_minor");
    auto semanticsRevision = requiredInteger(root, "effect_semantics_revision");
    auto classifier = requiredText(root, "classifier_sha256");
    auto gameVersion = requiredText(root, "game_version");
    auto bindings = requiredText(root, "bindings_commit");
    auto levelName = requiredText(root, "level_name");
    auto expectedSourceHash = requiredText(root, "source_editor_revision_sha256");
    auto expectedStrippedHash = requiredText(
        root, "expected_stripped_revision_sha256"
    );
    auto planHash = requiredText(root, "plan_sha256");
    auto sourceCountResult = requiredInteger(root, "source_object_count");
    auto retainedCountResult = requiredInteger(root, "retained_object_count");
    auto removedCountResult = requiredInteger(root, "removed_object_count");
    if (!schema || !major || !minor || !semanticsRevision || !classifier || !gameVersion ||
        !bindings || !levelName ||
        !expectedSourceHash || !expectedStrippedHash || !planHash ||
        !sourceCountResult || !retainedCountResult || !removedCountResult) {
        return Err("Removal plan header is incomplete");
    }
    if (schema.unwrap() != "gd-strip-full-decoration-removal-plan" ||
        major.unwrap() != 1 ||
        minor.unwrap() != kPlanSchemaMinor ||
        semanticsRevision.unwrap() != gd_native::kSemRev ||
        gameVersion.unwrap() != Loader::get()->getGameVersion() ||
        bindings.unwrap() != kBindingsCommit) {
        return Err("Removal plan targets a different bridge or game version");
    }
    GEODE_UNWRAP_INTO(auto currentClassifier, classifierHash());
    if (lowercaseASCII(classifier.unwrap()) !=
        lowercaseASCII(currentClassifier)) {
        return Err("Removal plan uses different classifier inputs");
    }
    if (lowercaseASCII(expectedSourceHash.unwrap()) != lowercaseASCII(sourceHash)) {
        return Err("Removal plan does not match the current editor revision");
    }
    for (auto key : {
             "all_records_classified",
             "effect_expansion_complete_for_revision",
             "conditioning_layout_effect_closure_complete",
             "exact_inverse_merge_proven",
             "whole_record_removal",
             "layer_agnostic_classification",
             "layer_exact_raw_restoration",
             "revision_locked",
             "eligible_for_editor_mutation",
         }) {
        GEODE_UNWRAP(requireTrue(root, key));
    }
    auto fragment = root["property_fragment_mutation"].asBool();
    if (!fragment || fragment.unwrap()) {
        return Err("Removal plan attempts property-fragment mutation");
    }
    auto unsupported = root["unsupported_uses"].asArray();
    if (!unsupported || !unsupported.unwrap().empty()) {
        return Err("Removal plan contains unsupported concrete effects");
    }

    auto partitionProof = root["layout_partition_proof"];
    GEODE_UNWRAP_INTO(auto partitionAlgorithm, requiredText(partitionProof, "algorithm"));
    GEODE_UNWRAP_INTO(auto provenRetainedCount, requiredInteger(partitionProof, "retained_object_count"));
    GEODE_UNWRAP_INTO(auto keptHashText, requiredText(partitionProof, "aggregate_retained_indices_sha256"));
    if (partitionAlgorithm != "immutable_piece_roots_exact_reverse_union") {
        return Err("Removal plan uses a different causal algorithm");
    }
    for (auto key : {
        "every_true_layout_piece_seeded", "root_identity_preserved",
        "monotone_reverse_effect_closure", "union_equals_independent_closures",
        "cross_piece_results_never_create_edges",
    }) GEODE_UNWRAP(requireTrue(partitionProof, key));
    auto pieceCount = partitionProof["piece_count"].asInt().unwrapOr(-1);
    if (pieceCount < 0 || partitionProof["seeded_piece_count"].asInt().unwrapOr(-2) != pieceCount ||
        provenRetainedCount < 0 || keptHashText.size() != 64) {
        return Err("Removal plan has an incomplete causal proof");
    }

    auto const sourceCountRaw = sourceCountResult.unwrap();
    auto const retainedCountRaw = retainedCountResult.unwrap();
    auto const removedCountRaw = removedCountResult.unwrap();
    if (sourceCountRaw < 0 || retainedCountRaw < 0 || removedCountRaw < 0 ||
        retainedCountRaw + removedCountRaw != sourceCountRaw ||
        provenRetainedCount != retainedCountRaw) {
        return Err("Removal plan object counts are invalid");
    }
    auto const sourceCount = static_cast<std::size_t>(sourceCountRaw);
    GEODE_UNWRAP_INTO(auto retained, parseRecords(root, "retained_records", sourceCount));
    GEODE_UNWRAP_INTO(auto removed, parseRecords(root, "removed_records", sourceCount));
    if (removed.size() != static_cast<std::size_t>(removedCountRaw) ||
        retained.size() + removed.size() != sourceCount) {
        return Err("Removal plan partition counts disagree");
    }
    std::vector<std::uint32_t> retainedIndices;
    retainedIndices.reserve(retained.size());
    for (auto const& row : retained) {
        if (row.serializationIndex > UINT32_MAX) {
            return Err("Removal plan retained index exceeds its proof encoding");
        }
        retainedIndices.push_back(static_cast<std::uint32_t>(row.serializationIndex));
    }
    std::sort(retainedIndices.begin(), retainedIndices.end());
    std::string retainedIndexBytes;
    retainedIndexBytes.reserve(retainedIndices.size() * sizeof(std::uint32_t));
    for (auto index : retainedIndices) {
        retainedIndexBytes.push_back(static_cast<char>(index & 0xff));
        retainedIndexBytes.push_back(static_cast<char>((index >> 8) & 0xff));
        retainedIndexBytes.push_back(static_cast<char>((index >> 16) & 0xff));
        retainedIndexBytes.push_back(static_cast<char>((index >> 24) & 0xff));
    }
    if (lowercaseASCII(geode::sha256(retainedIndexBytes).toString()) !=
        lowercaseASCII(keptHashText)) {
        return Err("Removal plan retained indices disagree with its set proof");
    }
    std::vector<std::optional<PlannedObjectRecord>> slots(sourceCount);
    for (auto const& row : retained) {
        if (slots[row.serializationIndex]) return Err("Removal plan repeats an index");
        slots[row.serializationIndex] = row;
    }
    for (auto const& row : removed) {
        if (slots[row.serializationIndex]) return Err("Removal plan repeats an index");
        slots[row.serializationIndex] = row;
    }
    std::vector<PlannedObjectRecord> fullRecords;
    fullRecords.reserve(sourceCount);
    for (auto& slot : slots) {
        if (!slot) return Err("Removal plan does not classify every record");
        fullRecords.push_back(std::move(*slot));
    }

    auto segments = splitPreservingEmpty(source, ';');
    std::vector<std::string> sourceObjects;
    for (std::size_t index = 1; index < segments.size(); ++index) {
        if (!segments[index].empty()) sourceObjects.push_back(segments[index]);
    }
    if (sourceObjects.size() != fullRecords.size()) {
        return Err("Removal plan source count differs from editor");
    }
    for (std::size_t index = 0; index < sourceObjects.size(); ++index) {
        if (sourceObjects[index] != fullRecords[index].exactSaveString) {
            return Err("Removal plan raw order differs from editor");
        }
    }

    std::set<std::size_t> removedIndices;
    for (auto const& row : removed) removedIndices.insert(row.serializationIndex);
    std::string expectedStripped = segments.front() + ";";
    for (std::size_t index = 0; index < fullRecords.size(); ++index) {
        if (!removedIndices.contains(index)) {
            expectedStripped += fullRecords[index].exactSaveString + ";";
        }
    }
    if (lowercaseASCII(geode::sha256(expectedStripped).toString()) !=
        lowercaseASCII(expectedStrippedHash.unwrap())) {
        return Err("Removal plan stripped revision proof is inconsistent");
    }

    return Ok(LoadedRemovalPlan{
        path,
        levelName.unwrap(),
        std::move(source),
        std::move(sourceHash),
        expectedStrippedHash.unwrap(),
        planHash.unwrap(),
        std::move(fullRecords),
        std::move(removed),
    });
}

Result<LoadedRemovalPlan> loadPlan(LevelEditorLayer* editor) {
    if (!editor || editor->m_playbackMode != PlaybackMode::Not) {
        return Err("Stop editor playtest before extracting layout");
    }
    auto source = copyString(editor->getLevelString());
    auto sourceHash = geode::sha256(source).toString();
    auto path = Mod::get()->getSaveDir() / "removal-plans" /
        fmt::format("{}.json", lowercaseASCII(sourceHash));
    GEODE_UNWRAP_INTO(auto plan, loadPlanFile(path, std::move(source)));
    if (!editor->m_level ||
        plan.levelName != copyString(editor->m_level->m_levelName)) {
        return Err("Removal plan belongs to a different saved level");
    }
    return Ok(std::move(plan));
}

Result<LoadedRemovalPlan> loadCapturedPlan(
    LevelEditorLayer* editor,
    std::string source
) {
    if (!editor || !editor->m_level ||
        editor->m_playbackMode != PlaybackMode::Not) {
        return Err("The extraction editor is unavailable");
    }
    auto sourceHash = geode::sha256(source).toString();
    auto path = Mod::get()->getSaveDir() / "removal-plans" /
        fmt::format("{}.json", lowercaseASCII(sourceHash));
    GEODE_UNWRAP_INTO(auto plan, loadPlanFile(path, std::move(source)));
    if (plan.levelName != copyString(editor->m_level->m_levelName)) {
        return Err("Removal plan belongs to a different extraction level");
    }
    return Ok(std::move(plan));
}

#ifdef GEODE_IS_WINDOWS
Result<std::filesystem::path> nativeWorker() {
    HMODULE module = nullptr;
    auto address = reinterpret_cast<LPCWSTR>(
        reinterpret_cast<std::uintptr_t>(&nativeWorker)
    );
    if (!GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            address, &module
        )) {
        return Err("The native worker module could not be located");
    }
    auto resource = FindResourceW(module, L"GD_WORKER", MAKEINTRESOURCEW(10));
    if (!resource) return Err("The packaged native worker is missing");
    auto loaded = LoadResource(module, resource);
    auto size = SizeofResource(module, resource);
    auto data = loaded ? LockResource(loaded) : nullptr;
    if (!data || !size) return Err("The packaged native worker is unreadable");
    auto bytes = std::string_view(static_cast<char const*>(data), size);
    auto expected = geode::sha256(bytes).toString();
    auto directory = Mod::get()->getSaveDir() / "native";
    auto path = directory / "gd-layout-worker.exe";
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    if (error) return Err(error.message());
    if (std::filesystem::is_regular_file(path)) {
        auto current = geode::sha256File(path);
        if (current && lowercaseASCII(current.unwrap().toString()) ==
            lowercaseASCII(expected)) {
            return Ok(path);
        }
    }
    auto temporary = path;
    temporary += ".tmp";
    {
        std::ofstream stream(temporary, std::ios::binary | std::ios::trunc);
        if (!stream) return Err("The native worker file could not be created");
        stream.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
        if (!stream) return Err("The native worker file could not be written");
    }
    if (!MoveFileExW(
            temporary.c_str(), path.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        )) {
        return Err(
            "The native worker file could not be published ({})", GetLastError()
        );
    }
    GEODE_UNWRAP_INTO(auto written, geode::sha256File(path));
    if (lowercaseASCII(written.toString()) != lowercaseASCII(expected)) {
        return Err("The extracted native worker failed its hash check");
    }
    return Ok(path);
}

std::wstring quoteWindowsArgument(std::wstring const& value) {
    std::wstring result = L"\"";
    std::size_t backslashes = 0;
    for (auto character : value) {
        if (character == L'\\') {
            ++backslashes;
            continue;
        }
        if (character == L'\"') {
            result.append(backslashes * 2 + 1, L'\\');
            result.push_back(L'\"');
            backslashes = 0;
            continue;
        }
        result.append(backslashes, L'\\');
        backslashes = 0;
        result.push_back(character);
    }
    result.append(backslashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}
#endif

struct ExtractProcess {
#ifdef GEODE_IS_WINDOWS
    HANDLE handle = nullptr;
    HANDLE job = nullptr;
    HANDLE phase = nullptr;
#endif
    unsigned long processID = 0;
};

Result<ExtractProcess> launchWorker(
    std::filesystem::path const& manifestPath
) {
#ifndef GEODE_IS_WINDOWS
    return Err("Live Extraction preparation is currently implemented only on Windows");
#else
    auto projectRoot = Mod::get()->getSaveDir() / "runtime";
    auto executable = projectRoot / "gd-layout.exe";
    if (!std::filesystem::is_regular_file(executable)) {
        return Err("The extraction runtime is unavailable");
    }
    auto command = quoteWindowsArgument(executable.wstring()) + L" extract " +
        quoteWindowsArgument(manifestPath.wstring()) + L" --game " +
        quoteWindowsArgument((geode::dirs::getGameDir() / "GeometryDash.exe").wstring());
    std::vector<wchar_t> mutableCommand(command.begin(), command.end());
    mutableCommand.push_back(L'\0');
    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    auto created = CreateProcessW(
        executable.c_str(), mutableCommand.data(), nullptr, nullptr, FALSE,
        CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr, projectRoot.c_str(),
        &startup, &process
    );
    if (!created) {
        return Err("Could not launch Extraction worker (Windows error {})", GetLastError());
    }
    auto job = CreateJobObjectW(nullptr, nullptr);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jobInfo{};
    jobInfo.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!job || !SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, &jobInfo, sizeof(jobInfo)
        ) || !AssignProcessToJobObject(job, process.hProcess)) {
        auto error = GetLastError();
        TerminateProcess(process.hProcess, 1);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        if (job) CloseHandle(job);
        return Err("Could not contain Extraction worker (Windows error {})", error);
    }
    if (ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
        auto error = GetLastError();
        TerminateProcess(process.hProcess, 1);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        CloseHandle(job);
        return Err("Could not start Extraction worker (Windows error {})", error);
    }
    CloseHandle(process.hThread);
    return Ok(ExtractProcess{
        process.hProcess, job, nullptr, process.dwProcessId
    });
#endif
}


Result<> writeAuditRecord(
    std::string_view levelName, std::string_view sourceHash,
    std::string_view strippedHash, std::string_view expectedStrippedHash,
    std::string_view planHash, std::size_t removedObjectCount,
    bool restored, std::string_view status
);

Result<> writeAudit(
    LoadedRemovalPlan const& plan,
    std::string const& strippedHash,
    bool restored,
    std::string_view status
);

Result<std::size_t> removeInPlace(
    LevelEditorLayer* editor,
    bool auditOutput,
    std::string const& emptyHash,
    std::string source
);

Result<> beginApply(ExactCopy copy, bool auditOutput);
Result<> finishLightPlan(
    LevelEditorLayer* editor,
    std::string source,
    std::string sourceHash
);

class JobWatcher final : public cocos2d::CCNode {
public:
    static JobWatcher* create(
        std::filesystem::path statusPath,
        std::string operationID,
        ExactCopy copy,
        bool auditOutput,
        bool lightOnly,
        ExtractProcess process
    ) {
        auto* watcher = new JobWatcher();
        if (!watcher || !watcher->init()) {
            CC_SAFE_DELETE(watcher);
            return nullptr;
        }
        watcher->m_statusPath = std::move(statusPath);
        watcher->m_operationID = std::move(operationID);
        watcher->m_levelName = std::move(copy.name);
        watcher->m_sourceHash = std::move(copy.sourceHash);
        watcher->m_source = std::move(copy.source);
        watcher->m_level = std::move(copy.level);
        watcher->m_editor = LevelEditorLayer::get();
        watcher->m_auditOutput = auditOutput;
        watcher->m_lightOnly = lightOnly;
        watcher->m_process = process;
        watcher->autorelease();
        watcher->retain();
#ifdef GEODE_IS_WINDOWS
        auto workerHandle = watcher->m_process.handle;
        auto workerJob = watcher->m_process.job;
        auto phaseEvent = watcher->m_process.phase;
        watcher->m_process.handle = nullptr;
        watcher->m_process.job = nullptr;
        watcher->m_process.phase = nullptr;
        std::thread([watcher, workerHandle, workerJob, phaseEvent] {
            HANDLE handles[] = {phaseEvent, workerHandle};
            DWORD waitResult = WAIT_FAILED;
            DWORD waitError = 0;
            while (true) {
                waitResult = WaitForMultipleObjects(2, handles, FALSE, INFINITE);
                if (waitResult == WAIT_OBJECT_0) {
                    watcher->retain();
                    geode::queueInMainThread([watcher] {
                        watcher->phase();
                        watcher->release();
                    });
                    continue;
                }
                if (waitResult == WAIT_FAILED) waitError = GetLastError();
                break;
            }
            DWORD exitCode = 1;
            if (waitResult == WAIT_OBJECT_0 + 1) {
                GetExitCodeProcess(workerHandle, &exitCode);
            }
            CloseHandle(phaseEvent);
            CloseHandle(workerHandle);
            CloseHandle(workerJob);
            geode::queueInMainThread([watcher, waitResult, waitError, exitCode] {
                watcher->complete(waitResult, waitError, exitCode);
            });
        }).detach();
#endif
        return watcher;
    }

    ~JobWatcher() override {
#ifdef GEODE_IS_WINDOWS
        if (m_process.handle) CloseHandle(m_process.handle);
        if (m_process.job) CloseHandle(m_process.job);
        if (m_process.phase) CloseHandle(m_process.phase);
#endif
    }

    void phase() {
        if (m_lightOnly) return;
        auto statusResult = geode::utils::file::readJson(m_statusPath);
        if (!statusResult) return;
        auto status = statusResult.unwrap();
        auto operation = status.contains("operation_id")
            ? status["operation_id"].asString().unwrapOr("") : "";
        if (operation != m_operationID) return;
        auto state = status.contains("state")
            ? status["state"].asString().unwrapOr("") : "";
        if (state == "building_native_projection") {
            showToggleNotice(
                "Analyzing Layout Primitives", NotificationIcon::Loading, 0.0f
            );
        }
        else if (state == "expanding_exact_mechanical_effects") {
            showToggleNotice(
                "Mapping Triggers", NotificationIcon::Loading, 0.0f
            );
        }
        else if (state == "closing_true_layout_pieces_independently") {
            showToggleNotice(
                "Mapping Dependencies", NotificationIcon::Loading, 0.0f
            );
        }
    }

    void complete(DWORD waitResult, DWORD waitError, DWORD exitCode) {
        if (waitResult != WAIT_OBJECT_0 + 1) {
            fail(fmt::format(
                "Waiting for the Extraction worker completion event failed ({})",
                waitError
            ));
            return;
        }
        auto statusResult = geode::utils::file::readJson(m_statusPath);
        if (!statusResult) {
            fail(fmt::format(
                "Extraction worker exited with code {} before publishing a terminal result",
                exitCode
            ));
            return;
        }
        auto status = statusResult.unwrap();
        auto operation = status.contains("operation_id")
            ? status["operation_id"].asString().unwrapOr("") : "";
        auto terminal = status.contains("terminal") &&
            status["terminal"].asBool().unwrapOr(false);
        if (operation != m_operationID || !terminal) {
            fail(fmt::format(
                "Extraction worker exited with code {} without the matching terminal result",
                exitCode
            ));
            return;
        }
        auto success = status.contains("success") &&
            status["success"].asBool().unwrapOr(false);
        auto message = status.contains("message")
            ? status["message"].asString().unwrapOr("") : "";
        if (!success || exitCode != 0) {
            fail(message.empty() ?
                fmt::format("Live Extraction worker failed with code {}", exitCode) :
                message);
            return;
        }
        if (m_lightOnly) {
            auto completed = finishLightPlan(
                m_editor, std::move(m_source),
                std::move(m_sourceHash)
            );
            if (!completed) fail(completed.unwrapErr());
            else {
                if (s_lightSync) s_lightSync();
                finish();
            }
            return;
        }
        writeToggleProgress(
            "on_demand_plan_ready", m_levelName, m_sourceHash, 0,
            std::nullopt, "Removal plan ready; opening the extracted level"
        );
        auto started = beginApply(ExactCopy{
            m_level, std::move(m_levelName), std::move(m_source),
            std::move(m_sourceHash)
        }, m_auditOutput);
        if (!started) {
            fail(started.unwrapErr());
            return;
        }
        finish();
    }

private:
    void fail(std::string const& message) {
        if (m_lightOnly) {
            s_lightBusy = false;
            s_lightError = message;
            clearSysLight();
            if (s_lightSync) s_lightSync();
            log::error("Orange preview {} failed: {}", m_operationID, message);
            finish();
            return;
        }
        log::error("Extraction job {} failed: {}", m_operationID, message);
        stopFlow(LevelEditorLayer::get(), "worker_failed");
        writeToggleFailure(message);
        showToggleNotice("Extraction failed", NotificationIcon::Error, 6.0f);
        finish();
    }

    void finish() {
        this->release();
    }

    std::filesystem::path m_statusPath;
    std::string m_operationID;
    std::string m_levelName;
    std::string m_sourceHash;
    std::string m_source;
    geode::Ref<GJGameLevel> m_level;
    LevelEditorLayer* m_editor = nullptr;
    bool m_auditOutput = false;
    bool m_lightOnly = false;
    ExtractProcess m_process;
};

Result<> startPlanBuild(
    ExactCopy copy,
    bool auditOutput,
    bool lightOnly = false
) {
    if (!copy.level) return Err("The extraction level is unavailable");
    if (copy.source.empty() || copy.sourceHash.empty()) {
        return Err("The captured extraction revision is unavailable");
    }
    auto operationID = s_toggleOperationID;
    auto root = Mod::get()->getSaveDir() / "decimal-jobs";
    auto rawPath = root / "raw" / fmt::format("{}.level", operationID);
    auto manifestPath = root / "jobs" / fmt::format("{}.json", operationID);
    auto statusPath = root / "statuses" / fmt::format("{}.json", operationID);
#ifdef GEODE_IS_WINDOWS
    GEODE_UNWRAP_INTO(auto workerPath, nativeWorker());
#endif
    auto nativeIndexPath = root / "native-indices" /
        fmt::format("{}.bin", operationID);
    auto nativeReportPath = root / "native-statuses" /
        fmt::format("{}.json", operationID);
#ifdef GEODE_IS_WINDOWS
    auto phaseEventName = fmt::format(
        "Local\\GDLayoutExtractor-{}", operationID
    );
#endif
    std::error_code error;
    std::filesystem::create_directories(rawPath.parent_path(), error);
    if (!error) std::filesystem::create_directories(manifestPath.parent_path(), error);
    if (!error) std::filesystem::create_directories(statusPath.parent_path(), error);
    if (!error) std::filesystem::create_directories(nativeIndexPath.parent_path(), error);
    if (!error) std::filesystem::create_directories(nativeReportPath.parent_path(), error);
    if (error) return Err(error.message());
    GEODE_UNWRAP_INTO(auto rawHash, geode::sha256File(rawPath));
    if (lowercaseASCII(rawHash.toString()) != lowercaseASCII(copy.sourceHash)) {
        return Err("The staged live-editor revision failed its hash check");
    }

    auto manifest = matjson::Value::object();
    manifest["schema"] = "gd-strip-decimal-job";
    manifest["schema_major"] = 1;
    manifest["operation_id"] = operationID;
    manifest["captured_unix_ms"] = unixMilliseconds();
    manifest["game_version"] = Loader::get()->getGameVersion();
    manifest["bindings_commit"] = std::string(kBindingsCommit);
    manifest["bridge_version"] = Mod::get()->getVersion().toNonVString();
    manifest["level_name"] = copy.name;
    manifest["level_id"] = static_cast<std::int64_t>(
        copy.level->m_levelID.value()
    );
    manifest["original_level_id"] = static_cast<std::int64_t>(
        copy.level->m_originalLevel.value()
    );
    manifest["level_index"] = static_cast<std::int64_t>(
        copy.level->m_levelIndex
    );
    manifest["source_revision_sha256"] = copy.sourceHash;
    manifest["source_size_bytes"] = static_cast<std::int64_t>(copy.source.size());
    manifest["raw_level_path"] = rawPath.string();
    manifest["status_path"] = statusPath.string();
#ifdef GEODE_IS_WINDOWS
    manifest["native_worker_path"] = workerPath.string();
#endif
    manifest["native_index_path"] = nativeIndexPath.string();
    manifest["native_report_path"] = nativeReportPath.string();
#ifdef GEODE_IS_WINDOWS
    manifest["phase_event_name"] = phaseEventName;
#endif
    GEODE_UNWRAP(geode::utils::file::writeStringSafe(manifestPath, manifest.dump()));

    auto status = matjson::Value::object();
    status["schema"] = "gd-strip-decimal-job-status";
    status["schema_major"] = 1;
    status["operation_id"] = operationID;
    status["level_name"] = copy.name;
    status["source_revision_sha256"] = copy.sourceHash;
    status["state"] = "live_editor_revision_staged";
    status["terminal"] = false;
    status["success"] = nullptr;
    status["message"] = "Live editor revision staged; semantic compilation is starting.";
    status["updated_unix_ms"] = unixMilliseconds();
    status["details"] = matjson::Value::object();
    auto serializedStatus = status.dump();
    GEODE_UNWRAP(geode::utils::file::writeStringSafe(statusPath, serializedStatus));
    GEODE_UNWRAP(geode::utils::file::writeStringSafe(
        root / "status-latest.json", serializedStatus
    ));

#ifdef GEODE_IS_WINDOWS
    auto phaseEventWide = std::wstring(
        phaseEventName.begin(), phaseEventName.end()
    );
    auto phaseEvent = CreateEventW(
        nullptr, FALSE, FALSE, phaseEventWide.c_str()
    );
    if (!phaseEvent) {
        return Err(
            "Could not create the extraction phase event (Windows error {})",
            GetLastError()
        );
    }
#endif
    auto processResult = launchWorker(manifestPath);
    if (!processResult) {
#ifdef GEODE_IS_WINDOWS
        CloseHandle(phaseEvent);
#endif
        return Err(processResult.unwrapErr());
    }
    auto process = processResult.unwrap();
#ifdef GEODE_IS_WINDOWS
    process.phase = phaseEvent;
#endif
    if (!JobWatcher::create(
            statusPath, operationID, std::move(copy), auditOutput,
            lightOnly, process
        )) {
#ifdef GEODE_IS_WINDOWS
        if (process.handle) CloseHandle(process.handle);
        if (process.job) CloseHandle(process.job);
        if (process.phase) CloseHandle(process.phase);
#endif
        return Err("The Extraction worker started, but its completion watcher failed");
    }
    if (!lightOnly) {
        writeToggleProgress(
            "on_demand_plan_compiling", s_toggleLevelName,
            s_toggleSourceHash, 0, std::nullopt,
            "Compiling layout effects from the live editor revision"
        );
    }
    return Ok();
}

Result<> stageRawCopy(std::string const& source) {
    auto operationID = s_toggleOperationID;
    auto rawPath = Mod::get()->getSaveDir() / "decimal-jobs" / "raw" /
        fmt::format("{}.level", operationID);
    std::error_code error;
    std::filesystem::create_directories(rawPath.parent_path(), error);
    if (error) return Err(error.message());
    GEODE_UNWRAP(geode::utils::file::writeStringSafe(rawPath, source));
    GEODE_UNWRAP_INTO(auto rawHash, geode::sha256File(rawPath));
    if (lowercaseASCII(rawHash.toString()) !=
        lowercaseASCII(geode::sha256(source).toString())) {
        return Err("The analysis file failed its revision hash check");
    }
    return Ok();
}

void writeSwitchProof(
    ExactCopy const& copy,
    std::int64_t started,
    std::int64_t finished,
    LevelEditorLayer* target
) {
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-duplicate-level-switch";
    root["schema_major"] = 1;
    root["operation_id"] = s_toggleOperationID;
    root["source_level_name"] = s_toggleLevelName;
    root["duplicate_level_name"] = copy.name;
    root["source_revision_sha256"] = copy.sourceHash;
    root["switch_started_unix_ms"] = started;
    root["switch_finished_unix_ms"] = finished;
    root["switch_notice_duration_ms"] = finished - started;
    root["one_second_minimum_satisfied"] = finished - started >= 1000;
    root["open_editor_saved_before_switch"] = s_openSaved;
    root["screen_switch_count"] = 1;
    root["capture_started_unix_ms"] = s_capStarted;
    root["capture_finished_unix_ms"] = s_capFinished;
    root["capture_lock_duration_ms"] = s_capFinished - s_capStarted;
    root["mapping_started_unix_ms"] = s_mapStarted;
    root["apply_started_unix_ms"] = s_applyStarted;
    root["capture_input_locked"] = s_capLocked;
    root["mapping_input_unlocked"] = s_mapUnlocked;
    root["apply_input_blocked"] = inputLocked(target);
    root["input_gate_kind"] = "screen_overlay";
    root["editor_input_state_preserved"] = editInputSame(target);
    auto path = Mod::get()->getSaveDir() / "toggle-audits" /
        "switch-latest.json";
    auto written = geode::utils::file::writeStringSafe(path, root.dump());
    if (!written) log::error("Duplicate switch proof failed: {}", written.unwrapErr());
}

Result<> saveEditor(LevelEditorLayer* editor) {
    if (!editor || !editor->m_level) return Ok();
    if (editor->m_playbackMode != PlaybackMode::Not) editor->onStopPlaytest();
    auto source = copyString(editor->getLevelString());
    if (source.empty()) return Err("The open editor revision is empty");
    auto* pause = EditorPauseLayer::create(editor);
    if (!pause) return Err("Geometry Dash could not create its save control");
    pause->saveLevel();
    editor->m_level->m_levelString = encodeLevelText(source);
    editor->m_level->m_hasBeenModified = true;
    auto* manager = LocalLevelManager::sharedState();
    if (!manager) return Err("Geometry Dash local levels are unavailable");
    manager->save();
    if (decodeLevelText(editor->m_level->m_levelString) != source) {
        return Err("Geometry Dash did not save the open editor revision");
    }
    return Ok();
}

void finishSwitch(
    LevelEditorLayer* heldEditor,
    ExactCopy copy,
    bool auditOutput,
    std::int64_t switchStarted,
    unsigned long waitResult,
    unsigned long waitError
) {
    bool editorHeld = true;
    auto finish = [&] {
        if (editorHeld && heldEditor) {
            heldEditor->release();
            editorHeld = false;
        }
    };
    auto fail = [&](std::string const& message) {
        stopFlow(LevelEditorLayer::get(), "switch_failed");
        writeToggleFailure(message);
        showToggleNotice("Extraction failed", NotificationIcon::Error, 6.0f);
        finish();
    };
    if (waitResult != WAIT_OBJECT_0) {
        fail(fmt::format(
            "The one-second switch timer failed with Windows error {}",
            waitError
        ));
        return;
    }
    auto switchFinished = unixMilliseconds();
    auto elapsed = switchFinished - switchStarted;
    if (elapsed < 1000) {
        fail("The switching gate completed before its verified one-second minimum");
        return;
    }
    if (heldEditor && LevelEditorLayer::get() != heldEditor) {
        fail("The open editor changed during level switching");
        return;
    }
    auto segments = splitPreservingEmpty(copy.source, ';');
    if (segments.empty() || segments.front().empty()) {
        fail("The captured level omitted its settings record");
        return;
    }
    if (heldEditor) {
        heldEditor->unscheduleUpdate();
        heldEditor->unscheduleAllSelectors();
        heldEditor->removeFromParentAndCleanup(true);
        finish();
    }
    auto empty = segments.front();
    empty.push_back(';');
    copy.level->m_levelString = encodeLevelText(empty);
    auto* targetScene = LevelEditorLayer::scene(copy.level.data(), false);
    copy.level->m_levelString = encodeLevelText(copy.source);
    auto* targetEditor = LevelEditorLayer::get();
    if (!targetScene || !targetEditor ||
        targetEditor->m_level != copy.level.data()) {
        fail("The duplicate editor could not be opened");
        return;
    }
    auto emptyLive = copyString(targetEditor->getLevelString());
    auto emptyHash = geode::sha256(emptyLive).toString();
    if (targetEditor->getAllObjects() &&
        targetEditor->getAllObjects()->count() != 0) {
        fail("The extraction duplicate did not open empty");
        return;
    }
    dropInputGate();
    auto held = holdInputGate(targetEditor, targetScene);
    if (!held) {
        fail(held.unwrapErr());
        return;
    }
    auto applying = moveFlow(
        FlowPhase::Switching, FlowPhase::Applying, targetEditor, true,
        "apply_locked"
    );
    if (!applying) {
        fail(applying.unwrapErr());
        return;
    }
    writeSwitchProof(copy, switchStarted, switchFinished, targetEditor);
    cocos2d::CCDirector::sharedDirector()->replaceScene(targetScene);
    log::info("Duplicate level opened after {} milliseconds", elapsed);
    writeToggleProgress(
        "duplicate_level_active", copy.name, copy.sourceHash, 0,
        std::nullopt,
        fmt::format("Duplicate level active after {} milliseconds", elapsed)
    );
    showToggleNotice("Applying Extraction", NotificationIcon::Loading, 0.0f);
    auto applied = removeInPlace(
        targetEditor, auditOutput, emptyHash, std::move(copy.source)
    );
    if (!applied) {
        stopFlow(targetEditor, "apply_failed");
        writeToggleFailure(applied.unwrapErr());
        showToggleNotice("Extraction failed", NotificationIcon::Error, 6.0f);
    }
}

Result<> beginExtract(
    ExactCopy copy,
    bool auditOutput
) {
    writeToggleProgress(
        "extract_copy_created", copy.name, copy.sourceHash, 0,
        std::nullopt, "A separate extraction level now owns the operation"
    );
    showToggleNotice(
        "Analyzing Layout Primitives", NotificationIcon::Loading, 0.0f
    );
    return startPlanBuild(std::move(copy), auditOutput);
}

Result<> beginApply(
    ExactCopy copy,
    bool auditOutput
) {
    auto* editor = LevelEditorLayer::get();
    s_applyStarted = unixMilliseconds();
    GEODE_UNWRAP(holdInputGate(editor));
    if (editor) {
        auto saved = saveEditor(editor);
        if (!saved) {
            freeInputGate();
            return Err(saved.unwrapErr());
        }
        s_openSaved = true;
        if (editor->m_level == copy.level.data()) {
            auto current = copyString(editor->getLevelString());
            auto currentHash = geode::sha256(current).toString();
            if (lowercaseASCII(currentHash) != lowercaseASCII(copy.sourceHash)) {
                auto retained = createExactCopy(
                    editor, "Before Extraction", "", std::string_view(current)
                );
                if (!retained) {
                    freeInputGate();
                    return Err(retained.unwrapErr());
                }
                writeToggleProgress(
                    "edited_output_retained", retained.unwrap().name,
                    currentHash, 0, std::nullopt,
                    "An edited extraction target was retained before replacement"
                );
            }
        }
        editor->retain();
    }
    auto switching = moveFlow(
        FlowPhase::Mapping, FlowPhase::Switching, editor, true,
        "switch_locked"
    );
    if (!switching) {
        freeInputGate();
        if (editor) editor->release();
        return Err(switching.unwrapErr());
    }
    showToggleNotice(
        "Switching to Duplicate Level", NotificationIcon::Loading, 0.0f
    );
    auto switchStarted = unixMilliseconds();
    auto* timer = CreateWaitableTimerW(nullptr, true, nullptr);
    if (!timer) {
        freeInputGate();
        if (editor) editor->release();
        return Err("The one-second switch timer could not be created");
    }
    LARGE_INTEGER due{};
    due.QuadPart = -10000000LL;
    if (!SetWaitableTimer(timer, &due, 0, nullptr, nullptr, false)) {
        auto error = GetLastError();
        CloseHandle(timer);
        freeInputGate();
        if (editor) editor->release();
        return Err("The one-second switch timer failed with Windows error {}", error);
    }
    std::thread([
        timer, editor, copy = std::move(copy), auditOutput, switchStarted
    ]() mutable {
        auto waitResult = WaitForSingleObject(timer, INFINITE);
        auto waitError = waitResult == WAIT_OBJECT_0 ? 0UL : GetLastError();
        CloseHandle(timer);
        geode::queueInMainThread([
            editor, copy = std::move(copy), auditOutput, switchStarted,
            waitResult, waitError
        ]() mutable {
            finishSwitch(
                editor, std::move(copy), auditOutput, switchStarted,
                waitResult, waitError
            );
        });
    }).detach();
    return Ok();
}

Result<> reorderObjects(
    LevelEditorLayer* editor,
    std::vector<GameObject*> const& desiredPointers
) {
    if (!editor || !editor->m_objects) return Err("Editor registry is unavailable");
    auto* desired = cocos2d::CCArray::createWithCapacity(editor->m_objects->count());
    std::set<GameObject*> used;
    for (auto* object : desiredPointers) {
        if (!object) return Err("A retained source object pointer is null");
        desired->addObject(object);
        used.insert(object);
    }
    for (auto* object : geode::cocos::CCArrayExt<GameObject*>(editor->m_objects)) {
        if (!object || used.contains(object)) continue;
        if (object->m_objectID != 749) {
            return Err("Deletion left an unplanned serialized object in the registry");
        }
        desired->addObject(object);
    }
    editor->m_objects->removeAllObjects();
    editor->m_objects->addObjectsFromArray(desired);
    for (std::size_t index = 0; index < desiredPointers.size(); ++index) {
        auto* actual = static_cast<GameObject*>(editor->m_objects->objectAtIndex(
            static_cast<unsigned int>(index)
        ));
        if (actual != desiredPointers[index]) {
            return Err("The primary object registry refused retained source order");
        }
    }
    return Ok();
}

void refresh(LevelEditorLayer* editor) {

    editor->recreateGroups();
    editor->optimizeMoveGroups();
    editor->dirtifyTriggers();
    editor->fullUpdateDisabledGroups();
    editor->updateLevelColors();
}

Result<> applyLayers(
    std::vector<PlannedObjectRecord> const& records,
    std::vector<GameObject*> const& objects
) {
    if (records.size() != objects.size()) {
        return Err("Editor-layer restoration count mismatch");
    }
    for (std::size_t index = 0; index < records.size(); ++index) {
        if (!objects[index]) return Err("Editor-layer restoration found a null object");
        GEODE_UNWRAP_INTO(auto pairs, objectPairs(records[index].exactSaveString));
        int layerOne = 0;
        int layerTwo = 0;
        for (auto const& pair : pairs) {
            if (pair.first != "20" && pair.first != "61") continue;
            std::size_t used = 0;
            int value = 0;
            try {
                value = std::stoi(pair.second, &used);
            }
            catch (...) {
                return Err("Retained object has an invalid editor layer");
            }
            if (used != pair.second.size() ||
                value < std::numeric_limits<short>::min() ||
                value > std::numeric_limits<short>::max()) {
                return Err("Retained object has an invalid editor layer");
            }
            if (pair.first == "20") layerOne = value;
            else layerTwo = value;
        }
        objects[index]->m_editorLayer = static_cast<short>(layerOne);
        objects[index]->m_editorLayer2 = static_cast<short>(layerTwo);
    }
    return Ok();
}

Result<> applyAngles(
    std::vector<PlannedObjectRecord> const& records,
    std::vector<GameObject*> const& objects
) {
    if (records.size() != objects.size()) {
        return Err("Retained angle record and object counts differ");
    }
    for (std::size_t index = 0; index < records.size(); ++index) {
        auto* object = objects[index];
        if (!object) return Err("A retained angle target is unavailable");
        GEODE_UNWRAP_INTO(auto pairs, objectPairs(records[index].exactSaveString));
        std::map<std::string, std::string> values;
        for (auto const& pair : pairs) values[pair.first] = pair.second;
        auto angle = [&values](std::string_view key) -> Result<std::optional<float>> {
            auto found = values.find(std::string(key));
            if (found == values.end()) return Ok(std::optional<float>{});
            try {
                std::size_t consumed = 0;
                auto value = std::stof(found->second, &consumed);
                if (consumed != found->second.size() || !std::isfinite(value)) {
                    return Err("Retained angle has trailing data");
                }
                if (value != 0.0f) {
                    value = std::nextafter(
                        value,
                        std::copysign(
                            std::numeric_limits<float>::infinity(), value
                        )
                    );
                    if (value < 0.0f) value -= 0.006f;
                }
                return Ok(std::optional<float>(value));
            }
            catch (...) {
                return Err("Retained angle is not a finite decimal");
            }
        };
        GEODE_UNWRAP_INTO(auto rotation, angle("6"));
        GEODE_UNWRAP_INTO(auto rotationX, angle("131"));
        GEODE_UNWRAP_INTO(auto rotationY, angle("132"));
        if (rotation) {
            object->setRotation(*rotation);
            object->m_startRotationX = *rotation;
            object->m_startRotationY = *rotation;
        }
        if (rotationX) {
            object->setRotationX(*rotationX);
            object->m_startRotationX = *rotationX;
        }
        if (rotationY) {
            object->setRotationY(*rotationY);
            object->m_startRotationY = *rotationY;
        }
    }
    return Ok();
}

std::vector<PlannedObjectRecord> retainedRecords(LoadedRemovalPlan const& plan) {
    std::set<std::size_t> removed;
    for (auto const& record : plan.removedRecords) removed.insert(record.serializationIndex);
    std::vector<PlannedObjectRecord> retained;
    retained.reserve(plan.fullRecords.size() - removed.size());
    for (auto const& record : plan.fullRecords) {
        if (!removed.contains(record.serializationIndex)) retained.push_back(record);
    }
    return retained;
}

std::vector<GameObject*> liveObjects(LevelEditorLayer* editor) {
    std::vector<GameObject*> objects;
    if (!editor || !editor->m_objects) return objects;
    objects.reserve(editor->m_objects->count());
    for (auto* object : geode::cocos::CCArrayExt<GameObject*>(editor->m_objects)) {
        if (object && object->m_objectID != 749) objects.push_back(object);
    }
    return objects;
}

bool lightMapCurrent(LevelEditorLayer* editor) {
    return s_lightMapped && editor == s_lightEditor &&
        s_lightObjects == (editor->m_objects ? editor->m_objects->count() : 0) &&
        s_lightUndos == (editor->m_undoObjects ? editor->m_undoObjects->count() : 0) &&
        s_lightRedos == (editor->m_redoObjects ? editor->m_redoObjects->count() : 0) &&
        s_lightUndo == (editor->m_undoObjects ? editor->m_undoObjects->lastObject() : nullptr) &&
        s_lightRedo == (editor->m_redoObjects ? editor->m_redoObjects->lastObject() : nullptr);
}

Result<> mapLightObjects(
    LoadedRemovalPlan const& plan,
    std::string const& currentHash
) {
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_level || !editor->m_editorUI) {
        return Err("The orange preview editor is unavailable");
    }
    std::vector<PlannedObjectRecord> liveRecords;
    if (s_session && editor == s_session->editor &&
        editor->m_level == s_session->level &&
        lowercaseASCII(currentHash) == lowercaseASCII(s_session->strippedHash) &&
        lowercaseASCII(plan.planHash) == lowercaseASCII(s_session->plan.planHash)) {
        liveRecords = retainedRecords(plan);
    }
    else if (lowercaseASCII(currentHash) == lowercaseASCII(plan.sourceHash)) {
        liveRecords = plan.fullRecords;
    }
    else {
        return Err("The orange preview plan is older than this editor revision");
    }
    auto objects = liveObjects(editor);
    if (objects.size() != liveRecords.size()) {
        return Err("The orange preview object order changed");
    }
    s_lightByIndex.assign(plan.fullRecords.size(), nullptr);
    s_lightIds.clear();
    s_lightIds.reserve(objects.size());
    for (std::size_t index = 0; index < liveRecords.size(); ++index) {
        auto serial = liveRecords[index].serializationIndex;
        if (serial >= s_lightByIndex.size()) {
            return Err("The orange preview contains an invalid object index");
        }
        s_lightByIndex[serial] = objects[index];
        s_lightIds.emplace(objects[index], serial);
    }
    s_lightEditor = editor;
    s_lightObjects = editor->m_objects ? editor->m_objects->count() : 0;
    s_lightUndos = editor->m_undoObjects ? editor->m_undoObjects->count() : 0;
    s_lightRedos = editor->m_redoObjects ? editor->m_redoObjects->count() : 0;
    s_lightUndo = editor->m_undoObjects ? editor->m_undoObjects->lastObject() : nullptr;
    s_lightRedo = editor->m_redoObjects ? editor->m_redoObjects->lastObject() : nullptr;
    s_lightMapPlan = plan.planHash;
    s_lightMapped = true;
    return Ok();
}

Result<std::size_t> applyLightPlan(
    LoadedRemovalPlan const& plan
) {
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_editorUI || !lightMapCurrent(editor) ||
        s_lightMapPlan != plan.planHash) {
        return Err("The orange preview needs the current editor revision");
    }
    std::set<std::size_t> desired;
    std::vector<std::size_t> selectedIds;
    auto* ui = editor->m_editorUI;
    auto addSelected = [&](GameObject* selected) {
        if (!selected) return;
        auto found = s_lightIds.find(selected);
        if (found != s_lightIds.end()) selectedIds.push_back(found->second);
    };
    if (auto* selected = ui->getSelectedObjects()) {
        for (auto* object : geode::cocos::CCArrayExt<GameObject*>(selected)) {
            addSelected(object);
        }
    }
    addSelected(ui->m_selectedObject);
    std::ranges::sort(selectedIds);
    selectedIds.erase(
        std::unique(selectedIds.begin(), selectedIds.end()), selectedIds.end()
    );
    if (!selectedIds.empty()) {
        if (!sysGraphReady(plan.sourceHash)) {
            return Err("The causal system graph is not ready");
        }
        GEODE_UNWRAP_INTO(auto system, querySysGraph(selectedIds));
        desired = std::set<std::size_t>(system.begin(), system.end());
    }
    std::vector<GameObject*> visible;
    visible.reserve(std::min(desired.size(), s_lightByIndex.size()));
    for (auto index : desired) {
        if (index < s_lightByIndex.size() && s_lightByIndex[index]) {
            visible.push_back(s_lightByIndex[index]);
        }
    }
    return applySysLight(visible, desired.size());
}

Result<> refreshLight() {
    if (!s_lightOn) return Ok();
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_level || editor->m_playbackMode != PlaybackMode::Not) {
        return Err("Open an editor and stop playtest before previewing parts");
    }
    auto* ui = editor->m_editorUI;
    auto* selected = ui ? ui->getSelectedObjects() : nullptr;
    if (!ui || (!ui->m_selectedObject && (!selected || selected->count() == 0))) {
        s_lightRefreshPending = false;
        s_lightError.clear();
        clearSysLight();
        return Ok();
    }
    if (s_lightBusy) return Ok();
    s_lightRefreshPending = false;
    s_lightError.clear();
    if (lightMapCurrent(editor)) {
        auto* plan = s_session && s_session->editor == editor &&
            s_session->plan.planHash == s_lightMapPlan
            ? &s_session->plan : s_lightPlan ? &*s_lightPlan : nullptr;
        if (plan && sysGraphReady(plan->sourceHash)) {
            GEODE_UNWRAP(applyLightPlan(*plan));
            return Ok();
        }
    }
    s_lightMapped = false;
    auto source = copyString(editor->getLevelString());
    auto sourceHash = geode::sha256(source).toString();
    if (s_session && editor == s_session->editor &&
        editor->m_level == s_session->level &&
        lowercaseASCII(sourceHash) == lowercaseASCII(s_session->strippedHash)) {
        s_lightEditor = editor;
        s_lightHash = sourceHash;
        s_lightError.clear();
        auto graph = loadSysGraph(
            s_session->plan.sourceHash, s_session->plan.fullRecords.size()
        );
        GEODE_UNWRAP(graph);
        GEODE_UNWRAP(mapLightObjects(s_session->plan, sourceHash));
        GEODE_UNWRAP(applyLightPlan(s_session->plan));
        return Ok();
    }
    if (s_lightPlan && s_lightEditor == editor &&
        lowercaseASCII(s_lightHash) == lowercaseASCII(sourceHash)) {
        s_lightError.clear();
        GEODE_UNWRAP(mapLightObjects(*s_lightPlan, sourceHash));
        GEODE_UNWRAP(applyLightPlan(*s_lightPlan));
        return Ok();
    }
    auto cached = loadPlan(editor);
    if (cached) {
        s_lightPlan = std::move(cached.unwrap());
        s_lightEditor = editor;
        s_lightHash = sourceHash;
        s_lightError.clear();
        auto graph = loadSysGraph(
            s_lightPlan->sourceHash, s_lightPlan->fullRecords.size()
        );
        if (graph) {
            GEODE_UNWRAP(mapLightObjects(*s_lightPlan, sourceHash));
            GEODE_UNWRAP(applyLightPlan(*s_lightPlan));
            return Ok();
        }
        s_lightPlan.reset();
    }
    if (s_lightBusy) return Ok();
    s_lightBusy = true;
    s_lightEditor = editor;
    s_lightHash = sourceHash;
    s_lightError.clear();
    clearSysLight();
    s_toggleOperationID = fmt::format(
        "{}-{}", unixMilliseconds(), sourceHash.substr(0, 12)
    );
    auto staged = stageRawCopy(source);
    if (!staged) {
        s_lightBusy = false;
        return Err(staged.unwrapErr());
    }
    ExactCopy copy{
        geode::Ref<GJGameLevel>(editor->m_level),
        copyString(editor->m_level->m_levelName),
        std::move(source), std::move(sourceHash)
    };
    auto started = startPlanBuild(std::move(copy), false, true);
    if (!started) s_lightBusy = false;
    return started;
}

Result<> finishLightPlan(
    LevelEditorLayer* editor,
    std::string source,
    std::string sourceHash
) {
    s_lightBusy = false;
    if (!s_lightOn) {
        clearSysLight();
        return Ok();
    }
    auto* current = LevelEditorLayer::get();
    if (!current || current != editor || !current->m_level) {
        clearSysLight();
        return Ok();
    }
    if (current->m_playbackMode != PlaybackMode::Not) {
        s_lightRefreshPending = true;
        return Ok();
    }
    auto liveHash = geode::sha256(
        copyString(current->getLevelString())
    ).toString();
    if (lowercaseASCII(liveHash) != lowercaseASCII(sourceHash)) {
        return refreshLight();
    }
    auto path = Mod::get()->getSaveDir() / "removal-plans" /
        fmt::format("{}.json", lowercaseASCII(sourceHash));
    GEODE_UNWRAP_INTO(auto plan, loadPlanFile(path, std::move(source)));
    s_lightPlan = std::move(plan);
    s_lightEditor = current;
    s_lightHash = std::move(sourceHash);
    s_lightError.clear();
    GEODE_UNWRAP(loadSysGraph(
        s_lightPlan->sourceHash, s_lightPlan->fullRecords.size()
    ));
    GEODE_UNWRAP(mapLightObjects(*s_lightPlan, s_lightHash));
    GEODE_UNWRAP(applyLightPlan(*s_lightPlan));
    return Ok();
}

void stopPreview() {
    s_lightOn = false;
    s_lightRefreshPending = false;
    s_lightError.clear();
    clearSysLight();
}

std::string textForRecords(
    LoadedRemovalPlan const& plan,
    std::vector<PlannedObjectRecord> const& records
) {
    auto sourceSegments = splitPreservingEmpty(plan.sourceLevelString, ';');
    if (sourceSegments.empty()) return {};
    std::string result = sourceSegments.front();
    result.push_back(';');
    for (auto const& record : records) {
        result += record.exactSaveString;
        result.push_back(';');
    }
    return result;
}

std::string expectedStripText(LoadedRemovalPlan const& plan) {
    return textForRecords(plan, retainedRecords(plan));
}

Result<StripLoadMatch> rebuildWorld(
    LevelEditorLayer* editor,
    std::vector<PlannedObjectRecord> const& records,
    std::string const& expected
) {
    if (!editor || !editor->m_editorUI) {
        return Err("The extraction editor is unavailable");
    }
    editor->m_editorUI->deselectAll();
    editor->removeAllObjects();
    auto setup = gd::string(expected);
    editor->createObjectsFromSetup(setup);
    auto* created = editor->getAllObjects();
    if (!created && !records.empty()) {
        return Err("Geometry Dash did not rebuild the selected objects");
    }
    std::vector<GameObject*> objects;
    objects.reserve(records.size());
    if (created) {
        for (auto* object : geode::cocos::CCArrayExt<GameObject*>(created)) {
            if (object && object->m_objectID != 749) objects.push_back(object);
        }
    }
    if (objects.size() != records.size()) {
        return Err(
            "Geometry Dash rebuilt {} serial objects instead of {}",
            objects.size(), records.size()
        );
    }
    for (std::size_t index = 0; index < objects.size(); ++index) {
        if (!objects[index] || objects[index]->m_objectID != records[index].objectID) {
            return Err("Geometry Dash changed a selected object identity");
        }
    }
    GEODE_UNWRAP(applyAngles(records, objects));
    GEODE_UNWRAP(applyLayers(records, objects));
    GEODE_UNWRAP(reorderObjects(editor, objects));
    refresh(editor);
    return verifyFreshStrip(expected, copyString(editor->getLevelString()));
}

Result<std::size_t> removeInPlace(
    LevelEditorLayer* editor,
    bool auditOutput,
    std::string const& emptyHash,
    std::string source
) {
    if (s_session) return Err("This level is already extracted");
    if (!editor || !editor->m_level || !editor->m_editorUI) {
        return Err("The extraction editor is unavailable");
    }
    GEODE_UNWRAP_INTO(auto plan, loadCapturedPlan(editor, std::move(source)));
    auto const removedCount = plan.removedRecords.size();
    auto const strippedLevelString = expectedStripText(plan);
    auto const strippedHash = geode::sha256(strippedLevelString).toString();
    if (lowercaseASCII(strippedHash) != lowercaseASCII(plan.expectedStrippedHash)) {
        return Err("The static deletion proof no longer matches its plan");
    }
    auto current = copyString(editor->getLevelString());
    if (lowercaseASCII(geode::sha256(current).toString()) !=
            lowercaseASCII(emptyHash) ||
        (editor->getAllObjects() && editor->getAllObjects()->count() != 0)) {
        return Err("The empty extraction copy changed during mapping");
    }
    auto keptRecords = retainedRecords(plan);
    auto loadProof = rebuildWorld(editor, keptRecords, strippedLevelString);
    auto liveStripped = copyString(editor->getLevelString());
    auto liveHash = geode::sha256(liveStripped).toString();
    if (!loadProof) {
        auto evidenceDir = Mod::get()->getSaveDir() / "toggle-audits";
        std::error_code evidenceError;
        std::filesystem::create_directories(evidenceDir, evidenceError);
        auto evidencePath = evidenceDir / fmt::format(
            "{}-rebuild.level", s_toggleOperationID
        );
        if (!evidenceError) {
            auto evidence = geode::utils::file::writeStringSafe(
                evidencePath, liveStripped
            );
            if (!evidence) {
                log::error("Rebuild evidence write failed: {}", evidence.unwrapErr());
            }
        }
        return Err(
            "The duplicate rebuild differed from its saved partition: {}; rebuilt state: {}",
            loadProof.unwrapErr(), evidencePath.string()
        );
    }
    auto* manager = LocalLevelManager::sharedState();
    if (!manager) return Err("Geometry Dash local levels are unavailable");
    editor->m_level->m_levelString = encodeLevelText(strippedLevelString);
    editor->m_level->m_hasBeenModified = true;
    manager->save();
    auto saved = decodeLevelText(editor->m_level->m_levelString);
    auto savedHash = geode::sha256(saved).toString();
    if (saved != strippedLevelString ||
        lowercaseASCII(savedHash) != lowercaseASCII(strippedHash)) {
        return Err("Geometry Dash did not preserve the extracted level revision");
    }
    if (auditOutput) {
        auto audit = writeAudit(
            plan, strippedHash, false, "passed_via_copy_output"
        );
        if (!audit) log::error("Extraction audit write failed: {}", audit.unwrapErr());
    }
    auto recoveryPath = recoveryJournalPath(plan, editor->m_level);
    GEODE_UNWRAP(writeRecoveryJournal(
        recoveryPath, plan, editor->m_level, liveHash, strippedHash, "stripped"
    ));
    stopFlow(editor, "operation_completed");
    writeToggleProgress(
        "removed_exact", plan.levelName, plan.sourceHash, removedCount,
        strippedHash,
        fmt::format(
            "Duplicate rebuild verified with {} linked-group labels, {} keyframe-path labels, {} editor-layer encodings, {} main color caches, and {} detail color caches normalized",
            loadProof.unwrap().groupRenumbers,
            loadProof.unwrap().keyPathRenumbers,
            loadProof.unwrap().editorLayerFixes,
            loadProof.unwrap().mainColorFixes,
            loadProof.unwrap().detailColorFixes
        )
    );
    showToggleNotice(
        "Level Extracted", NotificationIcon::Success, 3.0f
    );
    ToggleSession session;
    session.plan = std::move(plan);
    session.recoveryPath = std::move(recoveryPath);
    session.strippedLevelString = std::move(liveStripped);
    session.strippedHash = std::move(liveHash);
    session.editor = editor;
    session.level = editor->m_level;
    session.strippedStateSaved = true;
    s_session = std::move(session);
    return Ok(removedCount);
}

Result<> applyCleanup() {
    if (s_busy || s_lightBusy || s_flowPhase != FlowPhase::Idle) {
        return Err("Wait for the current level operation to finish");
    }
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_level || !editor->m_editorUI ||
        editor->m_playbackMode != PlaybackMode::Not) {
        return Err("Stop the editor playtest before cleanup");
    }
    auto before = copyString(editor->getLevelString());
    std::vector<PlannedObjectRecord> priorRecords;
    for (auto record : splitPreservingEmpty(before, ';') | std::views::drop(1)) {
        if (record.empty()) continue;
        GEODE_UNWRAP_INTO(auto pairs, objectPairs(record));
        auto id = std::ranges::find_if(pairs, [](auto const& pair) {
            return pair.first == "1";
        });
        if (id == pairs.end()) return Err("A level object has no asset ID");
        priorRecords.push_back({priorRecords.size(), std::stoi(id->second), record});
    }
    auto owned = s_session && editor == s_session->editor &&
        editor->m_level == s_session->level &&
        lowercaseASCII(geode::sha256(before).toString()) ==
            lowercaseASCII(s_session->strippedHash);
    clearSysLight();
    auto cleaned = cleanupWorld();
    if (!cleaned) {
        auto restored = rebuildWorld(editor, priorRecords, before);
        s_lightMapped = false;
        if (s_lightOn) s_lightRefreshPending = true;
        if (!restored) return Err("Cleanup failed: {}; rollback failed: {}",
            cleaned.unwrapErr(), restored.unwrapErr());
        return Err("Cleanup failed and was rolled back: {}", cleaned.unwrapErr());
    }
    auto after = copyString(editor->getLevelString());
    if (owned) {
        s_session->strippedLevelString = after;
        s_session->strippedHash = geode::sha256(after).toString();
        s_session->strippedStateSaved = false;
        GEODE_UNWRAP(writeRecoveryJournal(
            s_session->recoveryPath, s_session->plan, s_session->level,
            s_session->strippedHash,
            geode::sha256(decodeLevelText(editor->m_level->m_levelString)).toString(),
            "stripped"
        ));
    }
    s_lightMapped = false;
    if (s_lightOn) GEODE_UNWRAP(refreshLight());
    return Ok();
}

std::string flatText(std::string_view text) {
    std::string flat;
    for (auto value : text) {
        auto byte = static_cast<unsigned char>(value);
        if (std::isalnum(byte)) flat.push_back(
            static_cast<char>(std::tolower(byte))
        );
    }
    return flat;
}

bool hasText(cocos2d::CCNode* node, std::string_view key) {
    if (!node) return false;
    if (auto* label = geode::cast::typeinfo_cast<cocos2d::CCLabelBMFont*>(node)) {
        auto* value = label->getString();
        if (value && flatText(value) == key) return true;
    }
    if (!node->getChildren()) return false;
    for (auto* child : geode::cocos::CCArrayExt<cocos2d::CCNode*>(
             node->getChildren()
         )) {
        if (hasText(child, key)) return true;
    }
    return false;
}

CCMenuItemSpriteExtra* findButton(
    cocos2d::CCNode* node,
    std::string_view key
) {
    if (!node) return nullptr;
    if (auto* item = geode::cast::typeinfo_cast<CCMenuItemSpriteExtra*>(node)) {
        if (hasText(item, key)) return item;
    }
    if (!node->getChildren()) return nullptr;
    for (auto* child : geode::cocos::CCArrayExt<cocos2d::CCNode*>(
             node->getChildren()
         )) {
        if (auto* found = findButton(child, key)) return found;
    }
    return nullptr;
}

cocos2d::CCPoint menuPoint(
    CCMenuItemSpriteExtra* item,
    cocos2d::CCNode* menu
) {
    auto* parent = item ? item->getParent() : nullptr;
    if (!item || !parent || !menu) return {0.0f, 0.0f};
    return menu->convertToNodeSpace(
        parent->convertToWorldSpace(item->getPosition())
    );
}

std::string extractHint() {
    auto* editor = LevelEditorLayer::get();
    if (editor && s_session && editor == s_session->editor &&
        editor->m_level == s_session->level) {
        auto liveHash = geode::sha256(
            copyString(editor->getLevelString())
        ).toString();
        if (lowercaseASCII(liveHash) ==
            lowercaseASCII(s_session->strippedHash)) {
            return "Editing this level.";
        }
    }
    return "Will make a copy of the level to apply edits.";
}

ButtonSprite* setSprite(
    std::string_view label,
    float width,
    float height
) {
    auto text = std::string(label);
    auto* sprite = ButtonSprite::create(
        text.c_str(), static_cast<int>(std::round(width)), true,
        "bigFont.fnt", "GJ_button_04.png", height, 0.24f
    );
    if (!sprite) return nullptr;
    auto dark = cocos2d::ccColor3B{42, 42, 42};
    if (sprite->m_BGSprite) sprite->m_BGSprite->setColor(dark);
    if (sprite->m_subBGSprite) sprite->m_subBGSprite->setColor(dark);
    if (sprite->m_label) sprite->m_label->setColor({255, 255, 255});
    return sprite;
}

class ExtractScreen final : public geode::Popup {
public:
    ~ExtractScreen() override { s_lightSync = {}; }

    static ExtractScreen* create() {
        auto* screen = new ExtractScreen();
        if (!screen || !screen->initScreen()) {
            CC_SAFE_DELETE(screen);
            return nullptr;
        }
        screen->autorelease();
        return screen;
    }

private:
    bool initScreen() {
        if (!Popup::init(340.0f, 185.0f)) return false;
        setID("extractor-screen");
        setTitle("Layout Extractor", "goldFont.fnt", 0.72f, 18.0f);
        m_info = cocos2d::CCLabelBMFont::create("", "bigFont.fnt");
        if (!m_info) return false;
        m_info->setScale(0.30f);
        m_info->setWidth(930.0f);
        m_info->setAlignment(cocos2d::kCCTextAlignmentCenter);
        m_mainLayer->addChildAtPosition(m_info, geode::Anchor::Top, {0.0f, -57.0f});
        auto* extractSprite = setSprite("Extract", 180.0f, 34.0f);
        if (!extractSprite) return false;
        m_extract = CCMenuItemSpriteExtra::create(
            extractSprite, this, menu_selector(ExtractScreen::onExtract)
        );
        m_extract->setID("extract-button");
        m_buttonMenu->addChildAtPosition(m_extract, geode::Anchor::Center, {0.0f, -10.0f});
        auto* cleanupSprite = setSprite("Cleanup", 70.0f, 20.0f);
        if (!cleanupSprite) return false;
        m_cleanup = CCMenuItemSpriteExtra::create(
            cleanupSprite, this, menu_selector(ExtractScreen::onCleanup)
        );
        m_cleanup->setID("cleanup-button");
        m_buttonMenu->addChildAtPosition(m_cleanup, geode::Anchor::BottomLeft, {48.0f, 20.0f});
        m_lightBtn = CCMenuItemToggler::createWithStandardSprites(
            this, menu_selector(ExtractScreen::onLight), 0.45f
        );
        if (!m_lightBtn) return false;
        m_lightBtn->setID("preview-toggle");
        m_buttonMenu->addChildAtPosition(m_lightBtn, geode::Anchor::BottomRight, {-22.0f, 20.0f});
        m_lightText = cocos2d::CCLabelBMFont::create("Orange preview", "bigFont.fnt");
        if (!m_lightText) return false;
        m_lightText->setScale(0.28f);
        m_lightText->setAnchorPoint({1.0f, 0.5f});
        m_mainLayer->addChildAtPosition(m_lightText, geode::Anchor::BottomRight, {-42.0f, 20.0f});
        s_lightSync = [this] { syncState(); };
        syncState();
        return true;
    }

    void onLight(cocos2d::CCObject* sender) {
        if (s_busy) return;
        if (s_lightOn) stopPreview();
        else {
            s_lightOn = true;
            auto result = refreshLight();
            if (!result) {
                s_lightError = result.unwrapErr();
                log::error("Orange preview failed: {}", s_lightError);
            }
        }
        syncState(typeinfo_cast<CCMenuItemToggler*>(sender));
    }

    void onExtract(cocos2d::CCObject*) {
        if (s_busy || s_lightBusy) return;
        auto* editor = LevelEditorLayer::get();
        if (!editor || !editor->m_level) return;
        auto owned = s_session && editor == s_session->editor &&
            editor->m_level == s_session->level;
        if (owned && lowercaseASCII(geode::sha256(copyString(editor->getLevelString())).toString()) ==
            lowercaseASCII(s_session->strippedHash)) {
            onClose(nullptr);
            return;
        }
        clearSysLight();
        s_lightMapped = false;
        s_lightRefreshPending = s_lightOn;
        onClose(nullptr);
        auto result = extractLayout(false);
        if (!result) {
            log::error("Extraction failed: {}", result.unwrapErr());
            showToggleNotice("Extraction failed", NotificationIcon::Error, 4.0f);
        }
    }

    void onCleanup(cocos2d::CCObject*) {
        auto result = applyCleanup();
        if (!result) log::error("Cleanup failed: {}", result.unwrapErr());
        syncState();
    }

    void syncState(CCMenuItemToggler* skip = nullptr) {
        auto* editor = LevelEditorLayer::get();
        if (m_extract) m_extract->setEnabled(!s_busy && !s_lightBusy);
        if (m_cleanup) m_cleanup->setEnabled(!s_busy && !s_lightBusy &&
            editor && editor->m_playbackMode == PlaybackMode::Not);
        if (m_lightBtn) {
            m_lightBtn->setEnabled(!s_busy);
            if (m_lightBtn != skip) m_lightBtn->toggle(s_lightOn);
        }
        if (m_info) {
            auto state = s_busy ? "Extraction is running."
                : s_lightBusy ? "Mapping the selected object's system."
                : !s_lightError.empty() ? s_lightError : extractHint();
            auto text = fmt::format(
                "Extract keeps causal layout and its dependencies.\nSelect objects to inspect their systems in orange.\n{}", state
            );
            m_info->setString(text.c_str());
        }
    }

    CCMenuItemSpriteExtra* m_extract = nullptr;
    CCMenuItemSpriteExtra* m_cleanup = nullptr;
    CCMenuItemToggler* m_lightBtn = nullptr;
    cocos2d::CCLabelBMFont* m_info = nullptr;
    cocos2d::CCLabelBMFont* m_lightText = nullptr;
};

class ExtractPanel final : public cocos2d::CCNode {
public:
    static ExtractPanel* create(EditorPauseLayer* pause) {
        auto* panel = new ExtractPanel();
        if (!panel || !panel->initPanel(pause)) {
            CC_SAFE_DELETE(panel);
            return nullptr;
        }
        panel->autorelease();
        return panel;
    }

    std::size_t buttonCount() const {
        return m_button ? 1 : 0;
    }

private:
    bool initPanel(EditorPauseLayer* pause) {
        if (!CCNode::init() || !pause) return false;
        setID("extractor-panel");
        auto* regroup = findButton(pause, "regroup");
        auto* keys = findButton(pause, "keys");
        auto* menu = regroup ? regroup->getParent() : nullptr;
        auto* image = regroup ? regroup->getNormalImage() : nullptr;
        if (!regroup || !keys || !menu || !image) return false;
        auto regroupPos = menuPoint(regroup, menu);
        auto keysPos = menuPoint(keys, menu);
        auto gap = std::abs(keysPos.x - regroupPos.x);
        if (gap < 1.0f) return false;
        auto size = image->getContentSize();
        auto* sprite = setSprite("Extractor", size.width, size.height);
        if (!sprite) return false;
        m_button = CCMenuItemSpriteExtra::create(
            sprite, this, menu_selector(ExtractPanel::onOpen)
        );
        if (!m_button) return false;
        m_button->setID("extractor-open");
        m_button->setPosition({regroupPos.x - gap, regroupPos.y});
        m_button->setScaleX(regroup->getScaleX());
        m_button->setScaleY(regroup->getScaleY());
        menu->addChild(m_button);
        return true;
    }

    void onOpen(cocos2d::CCObject*) {
        auto* screen = ExtractScreen::create();
        if (!screen) {
            log::error("Extractor screen could not be created");
            return;
        }
        screen->show();
    }

    CCMenuItemSpriteExtra* m_button = nullptr;
};

bool altMatchesLevel(
    StoredAlt const& stored,
    GJGameLevel* level
) {
    if (!level || stored.levelName != copyString(level->m_levelName)) return false;
    auto const levelID = static_cast<std::int64_t>(level->m_levelID.value());
    auto const originalLevelID = static_cast<std::int64_t>(
        level->m_originalLevel.value()
    );
    if ((stored.levelID != 0 || levelID != 0) && stored.levelID != levelID) {
        return false;
    }
    if ((stored.originalLevelID != 0 || originalLevelID != 0) &&
        stored.originalLevelID != originalLevelID) {
        return false;
    }
    if (stored.levelID == 0 && levelID == 0 &&
        stored.originalLevelID == 0 && originalLevelID == 0 &&
        stored.levelIndex >= 0 && level->m_levelIndex >= 0 &&
        stored.levelIndex != level->m_levelIndex) {
        return false;
    }
    return true;
}

Result<bool> resumeExtract(LevelEditorLayer* editor, bool fresh = false) {
    if (!editor || !editor->m_level) return Ok(false);
    if (s_session && s_session->editor == editor &&
        s_session->level == editor->m_level) {
        return Ok(true);
    }
    if (s_session) s_session.reset();
    GEODE_UNWRAP_INTO(auto alternates, readStoredAlts());
    std::vector<StoredAlt*> candidates;
    for (auto& stored : alternates) {
        if (altMatchesLevel(stored, editor->m_level) &&
            (stored.state == "stripped" || stored.state == "edited")) {
            candidates.push_back(&stored);
        }
    }
    if (candidates.empty()) return Ok(false);
    auto hasStamped = std::ranges::any_of(
        candidates,
        [](StoredAlt const* stored) { return stored->hasWrittenAt; }
    );
    auto eligible = [hasStamped](StoredAlt const* stored) {
        return !hasStamped || stored->hasWrittenAt;
    };
    auto* stored = *std::ranges::max_element(
        candidates,
        [&eligible](StoredAlt const* left, StoredAlt const* right) {
            if (!eligible(left)) return eligible(right);
            if (!eligible(right)) return false;
            return left->writtenAt < right->writtenAt;
        }
    );
    auto sameNewest = std::ranges::count_if(
        candidates,
        [stored, &eligible](StoredAlt const* candidate) {
            return eligible(candidate) && candidate->writtenAt == stored->writtenAt;
        }
    );
    if (sameNewest != 1) {
        return Err("The extracted level has no unique newest control state");
    }
    auto planPath = Mod::get()->getSaveDir() / "removal-plans" /
        fmt::format("{}.json", lowercaseASCII(stored->sourceHash));
    GEODE_UNWRAP_INTO(
        auto plan,
        loadPlanFile(planPath, stored->sourceLevelString)
    );
    if (!stored->planHash.empty() &&
        lowercaseASCII(stored->planHash) != lowercaseASCII(plan.planHash)) {
        return Err("The extracted level control state has a different plan");
    }
    auto records = retainedRecords(plan);
    auto programText = textForRecords(plan, records);
    auto programHash = !stored->liveStrippedHash.empty()
        ? stored->liveStrippedHash
        : (!stored->expectedStrippedHash.empty()
            ? stored->expectedStrippedHash
            : geode::sha256(programText).toString());
    auto currentText = copyString(editor->getLevelString());
    auto savedText = decodeLevelText(editor->m_level->m_levelString);
    if (fresh && stored->state == "stripped" &&
        !stored->savedLevelStringHash.empty() &&
        lowercaseASCII(geode::sha256(savedText).toString()) ==
            lowercaseASCII(stored->savedLevelStringHash)) {
        auto savedRows = splitPreservingEmpty(savedText, ';');
        auto objects = liveObjects(editor);
        auto savedRecords = records;
        savedRows.erase(savedRows.begin());
        std::erase(savedRows, std::string{});
        if (savedRows.size() != savedRecords.size() ||
            objects.size() != savedRecords.size()) {
            return Err("Saved extraction object count changed on load");
        }
        for (std::size_t index = 0; index < savedRecords.size(); ++index) {
            if (objects[index]->m_objectID != savedRecords[index].objectID) {
                return Err("Saved extraction object order changed on load");
            }
            savedRecords[index].exactSaveString = savedRows[index];
        }
        GEODE_UNWRAP(applyAngles(savedRecords, objects));
        currentText = copyString(editor->getLevelString());
        GEODE_UNWRAP(verifyFreshStrip(savedText, currentText));
        programHash = geode::sha256(currentText).toString();
    }
    if (lowercaseASCII(geode::sha256(currentText).toString()) ==
        lowercaseASCII(programHash)) {
        programText = std::move(currentText);
    }
    ToggleSession session;
    session.plan = std::move(plan);
    session.recoveryPath = stored->path;
    session.strippedLevelString = std::move(programText);
    session.strippedHash = std::move(programHash);
    session.editor = editor;
    session.level = editor->m_level;
    session.strippedStateSaved = true;
    s_session = std::move(session);
    return Ok(true);
}

Result<> writePanelState(
    LevelEditorLayer* editor,
    bool attached,
    std::size_t buttonCount,
    std::string_view state
) {
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-extractor-controls";
    root["level_name"] = editor && editor->m_level
        ? copyString(editor->m_level->m_levelName) : std::string{};
    root["state"] = std::string(state);
    root["attached"] = attached;
    root["entry_button_count"] = static_cast<std::int64_t>(buttonCount);
    root["screen_button_count"] = 3;
    root["cleanup_button_id"] = "cleanup-button";
    root["cleanup_position"] = "bottom_left";
    root["screen_blurb"] = extractHint();
    root["written_unix_ms"] = unixMilliseconds();
    auto directory = Mod::get()->getSaveDir() / "toggle-audits";
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    if (error) return Err(error.message());
    return geode::utils::file::writeStringSafe(
        directory / "extractor-controls.json", root.dump()
    );
}

Result<> writeAudit(
    LoadedRemovalPlan const& plan,
    std::string const& strippedHash,
    bool restored,
    std::string_view status
) {
    return writeAuditRecord(
        plan.levelName, plan.sourceHash, strippedHash,
        plan.expectedStrippedHash, plan.planHash,
        plan.removedRecords.size(), restored, status
    );
}

Result<> writeAuditRecord(
    std::string_view levelName,
    std::string_view sourceHash,
    std::string_view strippedHash,
    std::string_view expectedStrippedHash,
    std::string_view planHash,
    std::size_t removedObjectCount,
    bool restored,
    std::string_view status
) {
    if (expectedStrippedHash.empty() || planHash.empty()) {
        return Err("The durable decoration alternate lacks its plan proof");
    }
    auto root = matjson::Value::object();
    root["schema"] = "gd-strip-decoration-toggle-audit";
    root["schema_major"] = 1;
    root["game_version"] = Loader::get()->getGameVersion();
    root["bridge_version"] = Mod::get()->getVersion().toNonVString();
    root["level_name"] = std::string(levelName);
    root["source_revision_sha256"] = std::string(sourceHash);
    root["stripped_revision_sha256"] = std::string(strippedHash);
    root["expected_stripped_revision_sha256"] =
        std::string(expectedStrippedHash);
    root["live_stripped_matches_static_filter"] =
        lowercaseASCII(std::string(strippedHash)) ==
        lowercaseASCII(std::string(expectedStrippedHash));
    root["removal_verified_by_runtime_object_identity"] = true;
    root["plan_sha256"] = std::string(planHash);
    root["removed_object_count"] =
        static_cast<std::int64_t>(removedObjectCount);
    root["native_create_objects_from_setup_path_exercised"] = true;
    root["runtime_rebuild_verified"] = true;
    root["persisted_static_partition_verified"] = true;
    root["trigger_and_group_indexes_refreshed"] = true;
    root["status"] = std::string(status);
    auto directory = Mod::get()->getSaveDir() / "toggle-audits";
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    if (error) return Err(error.message());
    auto serialized = root.dump();
    GEODE_UNWRAP(geode::utils::file::writeStringSafe(
        directory / fmt::format(
            "{}.json", lowercaseASCII(std::string(sourceHash))
        ), serialized
    ));
    return geode::utils::file::writeStringSafe(directory / "latest.json", serialized);
}

}

void addExtractMenu(EditorPauseLayer* pause) {
    auto* editor = LevelEditorLayer::get();
    if (!pause || !editor ||
        pause->getChildByID("extractor-panel")) {
        return;
    }
    if (!s_busy && (!s_session || s_session->editor != editor ||
        s_session->level != editor->m_level)) {
        auto resumed = resumeExtract(editor);
        if (!resumed) {
            log::error("Extraction state could not be restored: {}", resumed.unwrapErr());
        }
    }
    auto* panel = ExtractPanel::create(pause);
    if (!panel) {
        log::error("Extraction controls could not be created");
        auto state = writePanelState(editor, false, 0, "attach_failed");
        if (!state) log::error("Extraction control status failed: {}", state.unwrapErr());
        return;
    }
    pause->addChild(panel, 10000);
    auto count = panel->buttonCount();
    auto attached = pause->getChildByID("extractor-panel") == panel && count == 1;
    auto state = writePanelState(
        editor, attached, count, attached ? "attached" : "verify_failed"
    );
    if (!state) log::error("Extraction control status failed: {}", state.unwrapErr());
}

void resumeEditor(LevelEditorLayer* editor) {
    if (s_busy) return;
    auto resumed = resumeExtract(editor, true);
    if (!resumed) log::error("Extraction state could not resume: {}",
        resumed.unwrapErr());
}

void clearPreview() {
    stopPreview();
}

void onLightSelect() {
    if (!s_lightOn) return;
    s_lightRefreshPending = true;
    auto* ui = EditorUI::get();
    auto* selected = ui ? ui->getSelectedObjects() : nullptr;
    if (!ui || (!ui->m_selectedObject && (!selected || selected->count() == 0))) clearSysLight();
}

void onLightUpdate() {
    if (!s_lightOn || !s_lightRefreshPending || s_lightBusy || s_busy) return;
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_level ||
        editor->m_playbackMode != PlaybackMode::Not) return;
    auto refreshed = refreshLight();
    if (!refreshed) {
        s_lightError = refreshed.unwrapErr();
        clearSysLight();
        log::error("Live orange preview failed: {}", s_lightError);
    }
    if (s_lightSync) s_lightSync();
}

void onLightEdit() {
    auto* editor = LevelEditorLayer::get();
    if (!editor || lightMapCurrent(editor)) return;
    s_lightMapped = false;
    if (s_lightOn) s_lightRefreshPending = true;
}

matjson::Value lightState() {
    auto state = matjson::Value::object();
    state["enabled"] = s_lightOn;
    state["busy"] = s_lightBusy;
    state["refresh_pending"] = s_lightRefreshPending;
    state["ready"] = s_lightOn && !s_lightBusy && !s_lightRefreshPending && s_lightMapped &&
        s_lightError.empty();
    state["error"] = s_lightError;
    state["source_revision_sha256"] = s_lightHash;
    return state;
}

ToggleStatus toggleStatus() {
    return {
        .busy = s_busy,
        .terminal = s_toggleTerminal,
        .success = s_toggleSuccess,
        .operationID = s_toggleOperationID,
        .action = s_toggleAction,
        .stage = s_toggleStage,
        .levelName = s_toggleLevelName,
        .sourceHash = s_toggleSourceHash,
        .resultHash = s_toggleResultHash,
        .message = s_toggleMessage,
    };
}

bool blocksInput() {
    return s_inputHeld;
}

Result<> extractLayout(bool auditOutput) {
    if (s_busy || s_flowPhase != FlowPhase::Idle) {
        ++s_flowRejects;
        markFlow(
            "overlap_rejected", s_flowPhase, FlowPhase::Capture,
            LevelEditorLayer::get(), false
        );
        return Err("A level operation is already working");
    }
    auto* editor = LevelEditorLayer::get();
    if (!editor) return Err("Open a level in the editor first");
    if (editor->m_playbackMode != PlaybackMode::Not) {
        return Err("Stop editor playtest before extraction");
    }
    s_toggleOperationID.clear();
    s_toggleAction.clear();
    s_toggleLevelName.clear();
    s_toggleSourceHash.clear();
    s_toggleObjectCount = 0;
    s_toggleStage.clear();
    s_toggleResultHash.clear();
    s_toggleMessage.clear();
    s_toggleTerminal = false;
    s_toggleSuccess = false;
    s_capStarted = 0;
    s_capFinished = 0;
    s_mapStarted = 0;
    s_applyStarted = 0;
    s_capLocked = false;
    s_mapUnlocked = false;
    s_openSaved = false;
    auto const requestedLevelName = editor->m_level
        ? copyString(editor->m_level->m_levelName) : std::string{};
    auto const requestedRevision = geode::sha256(
        copyString(editor->getLevelString())
    ).toString();
    writeToggleProgress(
        "request_started", requestedLevelName, requestedRevision, 0
    );
    s_session.reset();
    GEODE_UNWRAP(startFlow(FlowPhase::Capture, editor));
    showToggleNotice(
        "Copying Level to File for Analysis",
        NotificationIcon::Loading, 0.0f
    );
    geode::queueInMainThread([editor, auditOutput] {
        auto fail = [editor](std::string const& message) {
            stopFlow(editor, "capture_failed");
            writeToggleFailure(message);
            showToggleNotice("Extraction failed", NotificationIcon::Error, 6.0f);
        };
        if (LevelEditorLayer::get() != editor || !editor->m_level ||
            !editor->m_editorUI) {
            fail("The source editor changed before capture");
            return;
        }
        s_capStarted = unixMilliseconds();
        auto held = holdInputGate(editor);
        if (!held) {
            fail(held.unwrapErr());
            return;
        }
        auto locked = verifyFlow(
            FlowPhase::Capture, editor, true, "capture_locked"
        );
        if (!locked) {
            fail(locked.unwrapErr());
            return;
        }
        s_capLocked = inputLocked(editor);
        auto exact = copyString(editor->getLevelString());
        if (exact.empty()) {
            fail("The current source revision is empty");
            return;
        }
        auto* pause = EditorPauseLayer::create(editor);
        if (!pause) {
            fail("Geometry Dash could not create its save control");
            return;
        }
        pause->saveLevel();
        editor->m_level->m_levelString = encodeLevelText(exact);
        editor->m_level->m_hasBeenModified = true;
        auto* manager = LocalLevelManager::sharedState();
        if (!manager) {
            fail("Geometry Dash local levels are unavailable");
            return;
        }
        manager->save();
        if (decodeLevelText(editor->m_level->m_levelString) != exact) {
            fail("Geometry Dash did not preserve the source revision");
            return;
        }
        auto staged = stageRawCopy(exact);
        if (!staged) {
            fail(staged.unwrapErr());
            return;
        }
        auto copy = createExactCopy(
            editor, "Extracted", "", std::string_view(exact)
        );
        if (!copy) {
            fail(copy.unwrapErr());
            return;
        }
        if (copy.unwrap().source != exact) {
            fail("The duplicate differs from the captured analysis file");
            return;
        }
        freeInputGate();
        s_capFinished = unixMilliseconds();
        s_mapStarted = s_capFinished;
        s_mapUnlocked = !s_inputHeld;
        auto mapping = moveFlow(
            FlowPhase::Capture, FlowPhase::Mapping, editor, false,
            "mapping_unlocked"
        );
        if (!mapping) {
            fail(mapping.unwrapErr());
            return;
        }
        auto started = beginExtract(copy.unwrap(), auditOutput);
        if (!started) fail(started.unwrapErr());
    });
    return Ok();
}

Result<> recordStripSave(bool editorHadUndoActions) {
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_level) return Ok();

    if (!s_session) return Ok();
    if (editor != s_session->editor || editor->m_level != s_session->level) {
        return Err("The saved level does not own the active decoration alternate");
    }

    auto const liveHash = geode::sha256(
        copyString(editor->getLevelString())
    ).toString();
    auto const unchanged = lowercaseASCII(liveHash) ==
        lowercaseASCII(s_session->strippedHash);

    auto savedHash = geode::sha256(
        decodeLevelText(editor->m_level->m_levelString)
    ).toString();
    GEODE_UNWRAP(writeRecoveryJournal(
        s_session->recoveryPath, s_session->plan, s_session->level,
        s_session->strippedHash, savedHash,
        unchanged ? "stripped" : "edited"
    ));
    s_session->strippedStateSaved = true;
    log::info(
        "Saved {} level {}; extraction source is retained for inspection",
        unchanged ? "unchanged stripped" : "edited stripped",
        s_session->plan.levelName
    );
    return Ok();
}

void leaveToggle(bool saveCurrentState) {
    clearSysLight();
    if (!s_session) return;
    if (!saveCurrentState && !s_session->strippedStateSaved) {


        clearRecoveryJournal(s_session->recoveryPath);
    }

    s_session.reset();
}

}
