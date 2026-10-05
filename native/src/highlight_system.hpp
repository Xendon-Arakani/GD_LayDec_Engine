#pragma once

#include <Geode/Geode.hpp>

#include <set>
#include <string>
#include <vector>

namespace gd_strip {

geode::Result<std::size_t> applySysLight(
    std::vector<GameObject*> const& objects,
    std::size_t planned
);
void clearSysLight();
void recordSysSelect();
std::size_t sysLightCount();
geode::Result<> loadSysGraph(
    std::string const& sourceHash,
    std::size_t objectCount
);
geode::Result<std::vector<std::size_t>> querySysGraph(
    std::vector<std::size_t> const& selected
);
bool sysGraphReady(std::string const& sourceHash);

}
