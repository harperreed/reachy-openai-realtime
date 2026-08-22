# ABOUTME: Presence subsystem — the BOOTING/SLEEPING/WAKING/AWAKE state machine
# ABOUTME: and the manager that owns the sleep/wake session lifecycle (issue #12).
from .states import PresenceState, PresenceStateMachine

__all__ = ["PresenceState", "PresenceStateMachine"]
