"""Snapshot management controller for creating, reverting, listing, and forking VM snapshots."""

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, SupportsIndex, overload

from windows.qemu import find_qemu_img_binary

if TYPE_CHECKING:
    from windows.machine import Machine


class SnapshotError(RuntimeError):
    """Exception raised for snapshot management failures."""


@dataclass
class Snapshot:
    """Represents a virtual machine / disk snapshot with graph hierarchy and lifecycle actions."""

    id: str
    name: str
    date_sec: int | None = None
    vm_clock_sec: int | None = None
    vm_state_size: int = 0
    parent: "Snapshot | None" = None
    children: list["Snapshot"] = field(default_factory=list)
    controller: "SnapshotController | None" = field(default=None, repr=False, compare=False)

    @property
    def is_live(self) -> bool:
        """Return True if the snapshot includes live VM memory / CPU execution state."""
        return self.vm_state_size > 0

    @property
    def type(self) -> str:
        """Return 'live' (RAM + disk) or 'disk-only'."""
        return "live" if self.is_live else "disk-only"

    def revert(self, timeout: float = 60.0) -> None:
        """Revert the virtual machine to this snapshot."""
        if self.controller is None:
            raise SnapshotError(f"Snapshot '{self.name}' is not attached to an active machine controller.")
        self.controller.revert(self.name, timeout=timeout)

    def delete(self, timeout: float = 30.0) -> None:
        """Delete this snapshot from the virtual machine."""
        if self.controller is None:
            raise SnapshotError(f"Snapshot '{self.name}' is not attached to an active machine controller.")
        self.controller.delete(self.name, timeout=timeout)

    def fork(
        self,
        output_disk: "str | Path | None" = None,
        run: bool = False,
        **machine_kwargs: Any,
    ) -> "Machine":
        """Fork a new Machine instance starting from this snapshot."""
        if self.controller is None:
            raise SnapshotError(f"Snapshot '{self.name}' is not attached to an active machine controller.")
        return self.controller.fork(
            name_or_id=self.name,
            output_disk=output_disk,
            run=run,
            **machine_kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert snapshot metadata to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "is_live": self.is_live,
            "type": self.type,
            "date_sec": self.date_sec,
            "vm_clock_sec": self.vm_clock_sec,
            "vm_state_size": self.vm_state_size,
            "parent": self.parent.name if self.parent else None,
            "children": [c.name for c in self.children],
        }

    def __repr__(self) -> str:
        parent_name = f" parent={self.parent.name!r}" if self.parent else ""
        type_str = f" type={self.type!r}"
        size_str = f" vm_state_size={self.vm_state_size}" if self.is_live else ""
        return f"<Snapshot id={self.id!r} name={self.name!r}{type_str}{size_str}{parent_name}>"

    def __str__(self) -> str:
        return self.name


# Backward compatibility alias
SnapshotInfo = Snapshot


def _render_tree_node(
    node: Snapshot,
    prefix: str = "",
    is_last: bool = True,
    current_name: str | None = None,
    visited: set[str] | None = None,
) -> list[str]:
    """Helper to recursively render snapshot tree branches with ASCII formatting."""
    if visited is None:
        visited = set()

    lines: list[str] = []
    connector = "└── " if is_last else "├── "

    if node.name in visited:
        lines.append(f"{prefix}{connector}[{node.id}] {node.name} (cycle)")
        return lines
    visited.add(node.name)

    if node.is_live:
        state_str = f"live, {node.vm_state_size / (1024 * 1024):.1f} MB RAM"
    else:
        state_str = "disk-only"

    date_str = ""
    if node.date_sec:
        import datetime

        date_str = f", {datetime.datetime.fromtimestamp(node.date_sec).strftime('%Y-%m-%d %H:%M:%S')}"

    is_current = current_name is not None and (node.name == current_name or node.id == current_name)
    current_marker = " *" if is_current else ""
    lines.append(f"{prefix}{connector}[{node.id}] {node.name} ({state_str}{date_str}){current_marker}")

    child_prefix = prefix + ("    " if is_last else "│   ")
    for i, child in enumerate(node.children):
        is_child_last = i == len(node.children) - 1
        lines.extend(
            _render_tree_node(
                child,
                prefix=child_prefix,
                is_last=is_child_last,
                current_name=current_name,
                visited=visited,
            )
        )
    return lines


class SnapshotList(list[Snapshot]):
    """A list of Snapshot instances representing the snapshot hierarchy with tree rendering."""

    def __init__(
        self,
        snapshots: list[Snapshot],
        disk_name: str = "",
        current_name: str | None = None,
        controller: "SnapshotController | None" = None,
    ):
        super().__init__(snapshots)
        self.disk_name = disk_name
        self.current_name = current_name
        self._controller = controller

    @property
    def current(self) -> Snapshot | None:
        """Return the currently active / tip snapshot."""
        if not self:
            return None
        if self.current_name:
            for s in self:
                if s.name == self.current_name or s.id == self.current_name:
                    return s
        return self[-1]

    @property
    def parent(self) -> Snapshot | None:
        """Return the parent snapshot of the currently active / tip snapshot."""
        curr = self.current
        return curr.parent if curr else None

    @property
    def children(self) -> list[Snapshot]:
        """Return the children of the currently active / tip snapshot."""
        curr = self.current
        return curr.children if curr else []

    def revert(self, timeout: float = 60.0) -> None:
        """Revert the virtual machine to the current active / tip snapshot."""
        curr = self.current
        if not curr:
            raise SnapshotError("No snapshots available to revert to.")
        curr.revert(timeout=timeout)

    def delete(self, timeout: float = 30.0) -> None:
        """Delete the current active / tip snapshot."""
        curr = self.current
        if not curr:
            raise SnapshotError("No snapshots available to delete.")
        curr.delete(timeout=timeout)

    def fork(
        self,
        output_disk: "str | Path | None" = None,
        run: bool = False,
        **machine_kwargs: Any,
    ) -> "Machine":
        """Fork a new Machine instance from the current active / tip snapshot."""
        curr = self.current
        if not curr:
            raise SnapshotError("No snapshots available to fork from.")
        return curr.fork(output_disk=output_disk, run=run, **machine_kwargs)

    def get(self, name_or_id: str) -> Snapshot | None:
        """Retrieve snapshot metadata by name or snapshot ID."""
        for snap in self:
            if snap.name == name_or_id or snap.id == name_or_id:
                return snap
        return None

    @overload
    def __getitem__(self, key: SupportsIndex) -> Snapshot: ...

    @overload
    def __getitem__(self, key: slice) -> list[Snapshot]: ...

    @overload
    def __getitem__(self, key: str) -> Snapshot: ...

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return super().__getitem__(key)
        if isinstance(key, str):
            snap = self.get(key)
            if snap is None:
                raise KeyError(f"Snapshot '{key}' not found.")
            return snap
        raise TypeError(f"Invalid snapshot index type: {type(key)}")

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            return self.get(item) is not None
        return super().__contains__(item)

    def tree(self) -> str:
        """Return an ASCII tree representation of the snapshots."""
        header = f"Disk: {self.disk_name}" if self.disk_name else "Snapshots"
        if not self:
            return f"{header}\n└── (no snapshots)"

        lines = [header]
        roots = [s for s in self if s.parent is None]
        if not roots:
            roots = [self[0]]

        visited: set[str] = set()
        for idx, root in enumerate(roots):
            is_last = idx == len(roots) - 1
            lines.extend(
                _render_tree_node(
                    root,
                    prefix="",
                    is_last=is_last,
                    current_name=self.current_name or (self[-1].name if self else None),
                    visited=visited,
                )
            )
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.tree()

    def __repr__(self) -> str:
        return self.tree()


def _get_metadata_path(disk_path: Path) -> Path:
    return disk_path.parent / f".{disk_path.name}.snapshots.json"


def _list_snapshots_fallback(disk_path: Path) -> list[dict[str, Any]]:
    """Fallback listing parser using 'qemu-img snapshot -l'."""
    qemu_img = find_qemu_img_binary()
    cmd = [qemu_img, "snapshot", "-l", str(disk_path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return []

    snapshots: list[dict[str, Any]] = []
    lines = res.stdout.strip().splitlines()
    for line in lines[2:]:
        parts = line.split()
        if len(parts) >= 2:
            snapshots.append({"id": parts[0], "name": parts[1]})
    return snapshots


class SnapshotController:
    """Controller for taking, listing, reverting, deleting, and forking VM snapshots."""

    def __init__(self, machine: "Machine"):
        self._machine = machine
        self._metadata_path = _get_metadata_path(self.disk_path)

    @property
    def disk_path(self) -> Path:
        """Return the backing disk path of the machine's image."""
        return self._machine.image.disk_path

    def _load_metadata(self) -> dict[str, Any]:
        if self._metadata_path.exists():
            try:
                return json.loads(self._metadata_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"current": None, "parents": {}}

    def _save_metadata(self, data: dict[str, Any]) -> None:
        try:
            self._metadata_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    @property
    def current(self) -> Snapshot | None:
        """Return the current active / tip snapshot."""
        return self.list().current

    @property
    def parent(self) -> Snapshot | None:
        """Return the parent snapshot of the current active / tip snapshot."""
        return self.list().parent

    @overload
    def list(self, tree: Literal[False] = False) -> SnapshotList: ...

    @overload
    def list(self, tree: Literal[True]) -> str: ...

    @overload
    def list(self, tree: bool) -> SnapshotList | str: ...

    def list(self, tree: bool = False) -> SnapshotList | str:
        """List all snapshots present on the machine's disk image.

        Args:
            tree: If True, returns a formatted ASCII tree view string.

        Returns:
            A `SnapshotList` instance (whose `__repr__` and `__str__` render as an ASCII tree),
            or a formatted string if `tree=True`.
        """
        qemu_img = find_qemu_img_binary()
        cmd = [qemu_img, "info", "-U", "--output=json", str(self.disk_path)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(res.stdout)
            snapshots_data = data.get("snapshots", [])
        except Exception:
            snapshots_data = _list_snapshots_fallback(self.disk_path)

        meta = self._load_metadata()
        parents: dict[str, str | None] = meta.get("parents", {})
        current_name: str | None = meta.get("current")

        snap_map: dict[str, Snapshot] = {}
        ordered_names: list[str] = []

        for s in snapshots_data:
            s_name = str(s.get("name", ""))
            ordered_names.append(s_name)
            snap = Snapshot(
                id=str(s.get("id", "")),
                name=s_name,
                date_sec=s.get("date-sec"),
                vm_clock_sec=s.get("vm-clock-sec"),
                vm_state_size=s.get("vm-state-size", 0),
                controller=self,
            )
            snap_map[s_name] = snap

        # Connect parent/children hierarchy
        for idx, s_name in enumerate(ordered_names):
            s_obj = snap_map[s_name]
            parent_name = parents.get(s_name)
            if parent_name and parent_name in snap_map:
                parent_obj = snap_map[parent_name]
            elif s_name not in parents and idx > 0:
                # Chronological fallback
                prev_name = ordered_names[idx - 1]
                parent_obj = snap_map.get(prev_name)
            else:
                parent_obj = None

            if parent_obj is not None:
                s_obj.parent = parent_obj
                if s_obj not in parent_obj.children:
                    parent_obj.children.append(s_obj)

        snaps = list(snap_map.values())
        snap_list = SnapshotList(
            snaps,
            disk_name=self.disk_path.name,
            current_name=current_name,
            controller=self,
        )
        if tree:
            return snap_list.tree()
        return snap_list

    def tree(self) -> str:
        """Return an ASCII tree view representation of all snapshots on this machine."""
        return self.list(tree=False).tree()

    def get(self, name_or_id: str) -> Snapshot | None:
        """Retrieve snapshot metadata by name or snapshot ID."""
        for snap in self.list():
            if snap.name == name_or_id or snap.id == name_or_id:
                return snap
        return None

    def exists(self, name_or_id: str) -> bool:
        """Return True if a snapshot with the given name or ID exists."""
        return self.get(name_or_id) is not None

    def create(self, name: str, timeout: float = 60.0) -> Snapshot:
        """Create a new snapshot of the virtual machine.

        If the machine is currently running, saves both live VM memory state and disk state
        via QEMU monitor (`savevm`). If stopped, creates an internal disk snapshot via `qemu-img`.

        Args:
            name: Unique name/tag for the snapshot.
            timeout: Timeout in seconds for QEMU monitor savevm operation.

        Returns:
            Snapshot instance describing the newly created snapshot.
        """
        if self.exists(name):
            raise SnapshotError(f"Snapshot with name '{name}' already exists.")

        # Determine parent: current active snapshot
        meta = self._load_metadata()
        active_parent = meta.get("current")

        if self._machine.power.status == "running":
            output = self._machine.console.send_monitor_command(f"savevm {name}", timeout=timeout)
            if "Error" in output or "error" in output:
                raise SnapshotError(f"Failed to create live VM snapshot '{name}': {output.strip()}")
        else:
            qemu_img = find_qemu_img_binary()
            cmd = [qemu_img, "snapshot", "-c", name, str(self.disk_path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise SnapshotError(f"Failed to create disk snapshot '{name}': {res.stderr.strip()}")

        # Record parentage and update current
        meta.setdefault("parents", {})[name] = active_parent
        meta["current"] = name
        self._save_metadata(meta)

        snap = self.get(name)
        if snap is None:
            snap = Snapshot(id="unknown", name=name, controller=self)
        return snap

    def revert(self, name_or_id: str, timeout: float = 60.0) -> None:
        """Revert the virtual machine to a previously saved snapshot.

        If the machine is running, loads VM memory state and disk state via QEMU monitor (`loadvm`).
        If stopped, reverts the internal disk state via `qemu-img snapshot -a`.

        Args:
            name_or_id: Name or ID of the snapshot to revert to.
            timeout: Timeout in seconds for QEMU monitor loadvm operation.
        """
        if not self.exists(name_or_id):
            raise SnapshotError(f"Snapshot '{name_or_id}' does not exist.")

        snap = self.get(name_or_id)
        snap_name = snap.name if snap else name_or_id

        if self._machine.power.status == "running":
            output = self._machine.console.send_monitor_command(f"loadvm {snap_name}", timeout=timeout)
            if "Error" in output or "error" in output:
                raise SnapshotError(f"Failed to load snapshot '{snap_name}': {output.strip()}")
        else:
            qemu_img = find_qemu_img_binary()
            cmd = [qemu_img, "snapshot", "-a", snap_name, str(self.disk_path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise SnapshotError(f"Failed to revert to snapshot '{snap_name}': {res.stderr.strip()}")

        meta = self._load_metadata()
        meta["current"] = snap_name
        self._save_metadata(meta)

    def delete(self, name_or_id: str, timeout: float = 30.0) -> None:
        """Delete an existing snapshot.

        Args:
            name_or_id: Name or ID of the snapshot to delete.
            timeout: Timeout in seconds for QEMU monitor delvm operation.
        """
        if not self.exists(name_or_id):
            raise SnapshotError(f"Snapshot '{name_or_id}' does not exist.")

        snap = self.get(name_or_id)
        snap_name = snap.name if snap else name_or_id

        if self._machine.power.status == "running":
            output = self._machine.console.send_monitor_command(f"delvm {snap_name}", timeout=timeout)
            if "Error" in output or "error" in output:
                raise SnapshotError(f"Failed to delete snapshot '{snap_name}': {output.strip()}")
        else:
            qemu_img = find_qemu_img_binary()
            cmd = [qemu_img, "snapshot", "-d", snap_name, str(self.disk_path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise SnapshotError(f"Failed to delete snapshot '{snap_name}': {res.stderr.strip()}")

        meta = self._load_metadata()
        parents = meta.get("parents", {})
        deleted_parent = parents.pop(snap_name, None)
        # Reparent any children of deleted snapshot to its parent
        for child, p in list(parents.items()):
            if p == snap_name:
                parents[child] = deleted_parent
        if meta.get("current") == snap_name:
            meta["current"] = deleted_parent
        self._save_metadata(meta)

    def fork(
        self,
        name_or_id: str | None = None,
        output_disk: str | Path | None = None,
        run: bool = False,
        **machine_kwargs: Any,
    ) -> "Machine":
        """Create a new independent Machine instance from a snapshot ("fork").

        Extracts the snapshot state into a new standalone .qcow2 disk image using `qemu-img convert`
        and returns a new `Machine` instance referencing that disk.

        Args:
            name_or_id: Name or ID of the snapshot to fork from. If None, auto-creates a new snapshot.
            output_disk: Optional path for the new disk image. If None, auto-generates a unique filename.
            run: If True, powers on the newly created machine immediately.
            **machine_kwargs: Optional overrides for the new Machine instance (e.g. ram_mb, cpus, headless, etc.).

        Returns:
            A new `Machine` instance configured with the forked disk image.
        """
        from windows.image import Image
        from windows.machine import Machine

        if name_or_id is None:
            name_or_id = f"fork_snap_{uuid.uuid4().hex[:8]}"
            self.create(name_or_id)
        elif not self.exists(name_or_id):
            raise SnapshotError(f"Snapshot '{name_or_id}' does not exist on disk {self.disk_path}.")

        snap = self.get(name_or_id)
        snap_name = snap.name if snap else name_or_id

        if output_disk is None:
            random_suffix = uuid.uuid4().hex[:8]
            output_disk = self.disk_path.parent / f"{self.disk_path.stem}_fork_{snap_name}_{random_suffix}.qcow2"
        else:
            output_disk = Path(output_disk)

        output_disk = output_disk.resolve()
        output_disk.parent.mkdir(parents=True, exist_ok=True)

        qemu_img = find_qemu_img_binary()
        cmd = [
            qemu_img,
            "convert",
            "-U",
            "-f",
            "qcow2",
            "-O",
            "qcow2",
            "-l",
            snap_name,
            str(self.disk_path),
            str(output_disk),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise SnapshotError(f"Failed to fork disk image from snapshot '{snap_name}': {res.stderr.strip()}")

        forked_image = Image(output_disk)

        # Inherit defaults from current machine
        defaults: dict[str, Any] = {
            "ram_mb": self._machine.ram_mb,
            "cpus": self._machine.cpus,
            "headless": self._machine.headless,
            "username": self._machine.username,
            "password": self._machine.password,
        }
        defaults.update(machine_kwargs)

        forked_machine = Machine(forked_image, **defaults)
        if run:
            forked_machine.power.on()

        return forked_machine
