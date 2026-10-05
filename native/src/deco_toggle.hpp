#pragma once

#include <Geode/Geode.hpp>

namespace gd_strip {

struct ToggleStatus {
    bool busy = false;
    bool terminal = false;
    bool success = false;
    std::string operationID;
    std::string action;
    std::string stage;
    std::string levelName;
    std::string sourceHash;
    std::string resultHash;
    std::string message;
};

geode::Result<> extractLayout(bool auditOutput = false);
geode::Result<> recordStripSave(bool editorHadUndoActions);
void leaveToggle(bool saveCurrentState);
ToggleStatus toggleStatus();
void addExtractMenu(EditorPauseLayer* pause);
void resumeEditor(LevelEditorLayer* editor);
void clearPreview();
void onLightSelect();
void onLightUpdate();
void onLightEdit();
matjson::Value lightState();
bool blocksInput();

}
