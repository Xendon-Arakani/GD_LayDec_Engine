#include "prior_style.hpp"

#include <Geode/utils/cocos.hpp>
#include <Geode/utils/file.hpp>
#include <Geode/utils/hash.hpp>

#include <filesystem>
#include <set>
#include <string>
#include <string_view>
#include <vector>

using namespace geode::prelude;

namespace gd_strip {
namespace {

constexpr int whiteId = 1011;
constexpr cocos2d::ccColor3B white{255, 255, 255};
constexpr cocos2d::ccColor3B darkGrey{42, 42, 42};
constexpr cocos2d::ccHSVValue neutral{0.f, 1.f, 1.f, false, false};

struct ColorSig {
    int colorId;
    int defaultId;
    float hue;
    float saturation;
    float brightness;
    bool absSaturation;
    bool absBrightness;
    bool usesHsv;
    bool customBlend;
};

struct ArtSig {
    GameObject* object;
    ColorSig base;
    ColorSig detail;
};

std::string copyText(gd::string const& value) {
    return std::string(value.c_str(), value.size());
}

std::vector<std::string_view> splitText(
    std::string_view value,
    char split
) {
    std::vector<std::string_view> parts;
    std::size_t start = 0;
    while (start <= value.size()) {
        auto end = value.find(split, start);
        if (end == std::string_view::npos) {
            parts.push_back(value.substr(start));
            break;
        }
        parts.push_back(value.substr(start, end - start));
        start = end + 1;
    }
    return parts;
}

bool isStyleKey(std::string_view key) {
    static std::set<std::string_view> const keys = {
        "21", "22", "41", "42", "43", "44", "64", "67", "135", "155", "156"
    };
    return keys.contains(key);
}

bool actionIsBg(std::string_view action) {
    auto tokens = splitText(action, '_');
    for (std::size_t index = 0; index + 1 < tokens.size(); index += 2) {
        if (tokens[index] == "6" && tokens[index + 1] == "1000") return true;
    }
    return false;
}

std::string normColorSet(std::string_view value) {
    auto actions = splitText(value, '|');
    std::string result;
    for (auto action : actions) {
        if (!result.empty()) result.push_back('|');
        result.append(actionIsBg(action) ? "6_1000" : action);
    }
    return result;
}

std::string normSetup(std::string_view record) {
    auto tokens = splitText(record, ',');
    std::string result;
    for (std::size_t index = 0; index + 1 < tokens.size(); index += 2) {
        if (!result.empty()) result.push_back(',');
        result.append(tokens[index]);
        result.push_back(',');
        result.append(
            tokens[index] == "kS38" ? normColorSet(tokens[index + 1])
                                      : std::string(tokens[index + 1])
        );
    }
    if (tokens.size() % 2) {
        if (!result.empty()) result.push_back(',');
        result.append(tokens.back());
    }
    return result;
}

bool bgIsGrey(std::string_view record) {
    auto setup = splitText(record, ',');
    for (std::size_t index = 0; index + 1 < setup.size(); index += 2) {
        if (setup[index] != "kS38") continue;
        for (auto action : splitText(setup[index + 1], '|')) {
            if (!actionIsBg(action)) continue;
            bool red = false;
            bool green = false;
            bool blue = false;
            bool opaque = false;
            auto values = splitText(action, '_');
            for (std::size_t part = 0; part + 1 < values.size(); part += 2) {
                red |= values[part] == "1" && values[part + 1] == "42";
                green |= values[part] == "2" && values[part + 1] == "42";
                blue |= values[part] == "3" && values[part + 1] == "42";
                opaque |= values[part] == "7" && values[part + 1] == "1";
            }
            return red && green && blue && opaque;
        }
    }
    return false;
}

std::string omitStyle(std::string_view record) {
    auto tokens = splitText(record, ',');
    std::string result;
    for (std::size_t index = 0; index + 1 < tokens.size(); index += 2) {
        if (isStyleKey(tokens[index])) continue;
        if (!result.empty()) result.push_back(',');
        result.append(tokens[index]);
        result.push_back(',');
        result.append(tokens[index + 1]);
    }
    if (tokens.size() % 2) {
        if (!result.empty()) result.push_back(',');
        result.append(tokens.back());
    }
    return result;
}

Result<std::size_t> verifyStyle(
    std::string_view before,
    std::string_view after
) {
    auto oldParts = splitText(before, ';');
    auto newParts = splitText(after, ';');
    if (oldParts.size() != newParts.size()) {
        return Err("Visibility pass changed the object count");
    }
    if (oldParts.empty() || newParts.empty()) {
        return Err("Visibility pass lost level settings");
    }
    if (normSetup(oldParts.front()) != normSetup(newParts.front())) {
        return Err("Visibility pass changed level settings");
    }
    if (!bgIsGrey(newParts.front())) return Err("Visibility pass did not set a dark-grey background");
    std::size_t changed = 0;
    for (std::size_t index = 1; index < oldParts.size(); ++index) {
        if (oldParts[index] != newParts[index]) ++changed;
        if (omitStyle(oldParts[index]) != omitStyle(newParts[index])) {
            return Err("Visibility pass changed object {} mechanics", index - 1);
        }
    }
    return Ok(changed);
}

void setColor(GJSpriteColor* color) {
    if (!color) return;
    color->m_colorID = whiteId;
    color->m_opacity = 1.0f;
    color->m_baseOpacity = 1.0f;
    color->m_hsv = neutral;
    color->m_usesHSV = false;
    color->m_usesCustomBlend = false;
    color->m_customColor = white;
}

void setOpaque(GJSpriteColor* color) {
    if (!color) return;
    color->m_opacity = 1.0f;
    color->m_baseOpacity = 1.0f;
}

ColorSig readColor(GJSpriteColor* color) {
    if (!color) return {};
    return {
        color->m_colorID,
        color->m_defaultColorID,
        color->m_hsv.h,
        color->m_hsv.s,
        color->m_hsv.v,
        color->m_hsv.absoluteSaturation,
        color->m_hsv.absoluteBrightness,
        color->m_usesHSV,
        color->m_usesCustomBlend
    };
}

bool sameColor(ColorSig const& left, ColorSig const& right) {
    return left.colorId == right.colorId
        && left.defaultId == right.defaultId
        && left.hue == right.hue
        && left.saturation == right.saturation
        && left.brightness == right.brightness
        && left.absSaturation == right.absSaturation
        && left.absBrightness == right.absBrightness
        && left.usesHsv == right.usesHsv
        && left.customBlend == right.customBlend;
}

bool keepSourceArt(GameObjectType type) {
    switch (type) {
        case GameObjectType::InverseGravityPortal:
        case GameObjectType::NormalGravityPortal:
        case GameObjectType::ShipPortal:
        case GameObjectType::CubePortal:
        case GameObjectType::YellowJumpPad:
        case GameObjectType::PinkJumpPad:
        case GameObjectType::GravityPad:
        case GameObjectType::YellowJumpRing:
        case GameObjectType::PinkJumpRing:
        case GameObjectType::GravityRing:
        case GameObjectType::InverseMirrorPortal:
        case GameObjectType::NormalMirrorPortal:
        case GameObjectType::BallPortal:
        case GameObjectType::RegularSizePortal:
        case GameObjectType::MiniSizePortal:
        case GameObjectType::UfoPortal:
        case GameObjectType::DualPortal:
        case GameObjectType::SoloPortal:
        case GameObjectType::WavePortal:
        case GameObjectType::RobotPortal:
        case GameObjectType::TeleportPortal:
        case GameObjectType::GreenRing:
        case GameObjectType::DropRing:
        case GameObjectType::SpiderPortal:
        case GameObjectType::RedJumpPad:
        case GameObjectType::RedJumpRing:
        case GameObjectType::CustomRing:
        case GameObjectType::DashRing:
        case GameObjectType::GravityDashRing:
        case GameObjectType::SwingPortal:
        case GameObjectType::GravityTogglePortal:
        case GameObjectType::SpiderOrb:
        case GameObjectType::SpiderPad:
        case GameObjectType::TeleportOrb:
        case GameObjectType::Modifier:
        case GameObjectType::SecretCoin:
        case GameObjectType::Collectible:
        case GameObjectType::UserCoin:
        case GameObjectType::CollisionObject:
        case GameObjectType::Special:
        case GameObjectType::EnterEffectObject:
        case GameObjectType::AnimatedHazard:
            return true;
        default:
            return type != GameObjectType::Solid
                && type != GameObjectType::Hazard
                && type != GameObjectType::Decoration
                && type != GameObjectType::Breakable
                && type != GameObjectType::Slope;
    }
}

Result<> setGreyBg(LevelEditorLayer* editor) {
    if (!editor->m_levelSettings || !editor->m_levelSettings->m_effectManager) {
        return Err("Level color state is unavailable");
    }
    auto* manager = editor->m_levelSettings->m_effectManager;
    auto* action = manager->getColorAction(1000);
    if (!action) return Err("Background color state is unavailable");
    action->m_color = darkGrey;
    action->m_fromColor = darkGrey;
    action->m_toColor = darkGrey;
    action->m_currentOpacity = 1.0f;
    action->m_fromOpacity = 1.0f;
    action->m_toOpacity = 1.0f;
    action->m_blending = false;
    action->m_playerColor = 0;
    action->m_copyHSV = neutral;
    action->m_copyID = 0;
    action->m_copyColorCalculated = false;
    action->m_copyOpacity = false;
    action->m_copyColorLoop = false;
    action->m_legacyHSV = false;
    manager->colorActionChanged(action);
    manager->updateColorAction(action);
    editor->updateLevelColors();
    if (editor->m_background) {
        editor->m_background->setColor(darkGrey);
        editor->m_background->setOpacity(255);
    }
    return Ok();
}

Result<> savePriorRaw(std::string const& value) {
    auto hash = geode::sha256(value).toString();
    auto folder = Mod::get()->getSaveDir() / "priors" / "raw";
    std::error_code error;
    std::filesystem::create_directories(folder, error);
    if (error) return Err(error.message());
    return geode::utils::file::writeStringSafe(folder / (hash + ".level"), value);
}

Result<> writeAudit(matjson::Value const& value) {
    auto folder = Mod::get()->getSaveDir() / "priors";
    std::error_code error;
    std::filesystem::create_directories(folder, error);
    if (error) return Err(error.message());
    return geode::utils::file::writeStringSafe(
        folder / "status-latest.json", value.dump()
    );
}

Result<> failAudit(matjson::Value audit, std::string const& error) {
    audit["stage"] = "failed";
    audit["error"] = error;
    auto written = writeAudit(audit);
    if (!written) return written;
    return Err(error);
}

}

Result<> cleanupWorld() {
    auto* editor = LevelEditorLayer::get();
    if (!editor || !editor->m_level || !editor->getAllObjects()) {
        return Err("Open an editable level first");
    }
    if (editor->m_playbackMode != PlaybackMode::Not) {
        return Err("Stop the editor playtest first");
    }
    auto before = copyText(editor->getLevelString());
    auto audit = matjson::Value::object();
    audit["stage"] = "running";
    audit["level"] = copyText(editor->m_level->m_levelName);
    audit["before_sha256"] = geode::sha256(before).toString();
    GEODE_UNWRAP(writeAudit(audit));
    auto saved = savePriorRaw(before);
    if (!saved) return failAudit(audit, saved.unwrapErr());
    auto background = setGreyBg(editor);
    if (!background) return failAudit(audit, background.unwrapErr());
    std::size_t count = 0;
    std::size_t artCount = 0;
    std::size_t surfCount = 0;
    std::size_t noColorCount = 0;
    std::size_t mainCount = 0;
    std::size_t detailCount = 0;
    std::vector<ArtSig> artSigs;
    for (auto* object : geode::cocos::CCArrayExt<GameObject*>(editor->getAllObjects())) {
        if (!object) continue;
        ++count;
        auto keepArt = keepSourceArt(object->getType());
        auto mainColor = !keepArt && object->canChangeMainColor();
        auto detailColor = !keepArt && object->canChangeSecondaryColor();
        artCount += keepArt;
        surfCount += !keepArt;
        noColorCount += !keepArt && !mainColor && !detailColor;
        mainCount += mainColor;
        detailCount += detailColor;
        if (keepArt) {
            artSigs.push_back({
                object,
                readColor(object->m_baseColor),
                readColor(object->m_detailColor)
            });
        }
        setOpaque(object->m_baseColor);
        setOpaque(object->m_detailColor);
        if (mainColor) setColor(object->m_baseColor);
        if (detailColor) setColor(object->m_detailColor);
        object->m_isDontFade = true;
        object->m_isDontEnter = true;
        object->m_isHide = false;
        object->updateHSVState();
        object->updateBlendMode();
        object->updateObjectEditorColor();
    }
    editor->updateObjectColors(editor->getAllObjects());
    for (auto* object : geode::cocos::CCArrayExt<GameObject*>(editor->getAllObjects())) {
        if (!object) continue;
        auto keepArt = keepSourceArt(object->getType());
        auto mainColor = !keepArt && object->canChangeMainColor();
        auto detailColor = !keepArt && object->canChangeSecondaryColor();
        if (mainColor) object->setObjectColor(white);
        if (detailColor) object->setChildColor(white);
        if (mainColor || detailColor) object->setGlowColor(white);
        object->setGlowOpacity(255);
        if (mainColor) object->updateParticleColor(white);
        object->updateParticleOpacity(255);
        object->setOpacity(255);
        object->setVisible(true);
    }
    for (auto const& entry : artSigs) {
        if (!sameColor(entry.base, readColor(entry.object->m_baseColor))
            || !sameColor(entry.detail, readColor(entry.object->m_detailColor))) {
            return failAudit(audit, "Source-art color assignment changed");
        }
    }
    auto after = copyText(editor->getLevelString());
    auto verified = verifyStyle(before, after);
    if (!verified) return failAudit(audit, verified.unwrapErr());
    auto changed = verified.unwrap();
    audit["stage"] = "succeeded";
    audit["after_sha256"] = geode::sha256(after).toString();
    audit["objects"] = count;
    audit["changed_objects"] = changed;
    audit["source_art"] = artCount;
    audit["surface_objects"] = surfCount;
    audit["noncolorable"] = noColorCount;
    audit["white_main"] = mainCount;
    audit["white_detail"] = detailCount;
    audit["background_rgb"] = matjson::Value::array();
    for (auto value : {42, 42, 42}) audit["background_rgb"].push(value);
    audit["source_recolored"] = 0;
    audit["mechanics_preserved"] = true;
    audit["count_preserved"] = true;
    GEODE_UNWRAP(writeAudit(audit));
    editor->m_level->m_hasBeenModified = true;
    return Ok();
}

}
