"""The environment a scorer or media subprocess inherits from this process,
curated rather than inherited in full.

``CommandRunner.run`` used to build a child's environment as
``{**os.environ, **env}`` — every subprocess it launches (WhisperX, the
scorers, ffmpeg, the bell/person detectors) got this API process's *entire*
environment: ``DATABASE_URL`` (with credentials), ``AUTH_SECRET``, every
provider API key present in ``os.environ``, and anything else ``.env`` or the
platform set. The scoring layer already scopes what a subprocess needs with
real care — ``LLMSettingsService.subprocess_env_for`` builds one marker's
environment from a single credential snapshot and names only its own target
and key (see CLAUDE.md "Subprocess handoff") — but that scoping was undone at
the OS level the moment the runner merged it over a full ``os.environ``. A
crashed scorer's traceback, or a stray environment dump in a log, then hands
out every secret this deployment holds, not just the one the failing call
needed.

:func:`inherited_env` is the allowlist ``CommandRunner.run`` merges a call's
own ``env=`` dict over, in place of ``os.environ``. Everything a subprocess
actually needs beyond this list already arrives through that dict — see
``Settings.subprocess_env`` (PATH augmentation, ``FFMPEG_BIN``), the LLM
routing/credential env builders, and each engine's own ``python_env()``.
"""

from __future__ import annotations

import os

# Exact names inherited verbatim, matched case-insensitively — Windows'
# os.environ preserves whatever casing the OS stored a name in ("Path",
# "SystemRoot"), which is not always all-caps. Deliberately not a bare
# "NVIDIA_" prefix: that would silently re-admit NVIDIA_API_KEY, which every
# scoring call site already forwards on purpose, and only to the subprocess
# that is actually calling NVIDIA. Proxy variables are conventionally
# lowercase (some libraries check only that spelling), which case-insensitive
# matching also covers without a second set of entries.
INHERITED_NAMES: frozenset[str] = frozenset(
    name.upper()
    for name in (
        # Process plumbing every interpreter needs to start at all.
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        # Which physical accelerator(s) a subprocess may see — host/deployment
        # tuning, not a credential.
        "NVIDIA_VISIBLE_DEVICES",
        "CUDA_VISIBLE_DEVICES",
        # Outbound-HTTPS plumbing for a network behind a corporate proxy.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
)

# Prefixes covering accelerator, ML-framework and model-cache configuration:
# none of it a credential, all of it needed for a subprocess to find its
# weights and run on the right device.
INHERITED_PREFIXES: tuple[str, ...] = (
    "CUDA_",
    "HF_",
    "HUGGINGFACE_",
    "TRANSFORMERS_",
    "TORCH_",
    "PYTORCH_",
    "NEMO_",
    "OMP_",
    "MKL_",
    "XDG_",
)


def inherited_env() -> dict[str, str]:
    """The subset of this process's environment every subprocess may inherit.

    Everything else — ``DATABASE_URL``, ``AUTH_SECRET``, provider API keys,
    anything else ``.env`` sets — reaches a subprocess only if the caller puts
    it in the ``env=`` dict handed to ``CommandRunner.run``.
    """
    inherited: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in INHERITED_NAMES or upper.startswith(INHERITED_PREFIXES):
            inherited[name] = value
    return inherited
