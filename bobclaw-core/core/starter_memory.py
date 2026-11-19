"""Owner-only onboarding scaffold. No product knowledge or invented preferences."""
import os
import stat

STARTER_MEMORY = """<!-- BOB_OWNER_STARTER v1 -->
# About you (optional)
Fill in only what you choose to tell Bob.

## What to call you

## What you mainly use Bob for

## How you like answers

## Things Bob should never do without asking

## This PC and where your work lives

<!-- BOB_OWNER_STARTER_END -->
"""


def seed_empty(fd):
    """Caller holds the provisioning file lock; preserve every existing byte."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('unsafe lane memory file')
    if info.st_size:
        return False
    data = memoryview(STARTER_MEMORY.encode('utf-8'))
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise OSError('starter write failed')
        data = data[written:]
    return True


def starter_empty_for_owner(owner):
    """Small invocation-only boolean, never expose file content or other owners."""
    from core.lane_config import configured_mode, get_lane
    if not configured_mode() or not owner or not hasattr(os, 'O_NOFOLLOW'):
        return None
    lane = get_lane()
    if lane.root.parent.name != owner:
        return None
    target = lane.root.parent / 'MEMORY.md'
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            expected = STARTER_MEMORY.encode('utf-8')
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            return info.st_size == len(expected) and os.read(fd, len(expected) + 1) == expected
        finally:
            os.close(fd)
    except OSError:
        return None
