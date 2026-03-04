from .ComponentClass import Component


class ComponentList:
    def __init__(self):
        self.components: list[Component] = []

    def add(self, comp: Component):
        self.components.append(comp)

    def count(self) -> int:
        return len(self.components)

    def get_pkg_id_map(self) -> dict[str, Component]:
        """
        Return a dict mapping every supported pkg_id to its Component.

        When a component has multiple supported origins (rare), each origin
        produces its own pkg_id entry pointing to the same Component object.
        When multiple components share the same pkg_id (also rare), the last
        one wins — the scorecard result is identical for both.
        """
        pkg_map: dict[str, Component] = {}
        for comp in self.components:
            for pkg_id, _ in comp.get_supported_origins():
                pkg_map[pkg_id] = comp
        return pkg_map

    def get_unsupported(self) -> list[Component]:
        """Return components that have no supported ecosystem origins."""
        return [c for c in self.components if not c.get_supported_origins()]
