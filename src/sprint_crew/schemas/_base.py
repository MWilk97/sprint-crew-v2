"""Shared Pydantic config for every schema in this package.

``extra="forbid"`` is the house rule: a typo'd field in a request body is a client bug and
should surface as a 422, not be silently dropped. It was previously spelled out once per
schema module, which let the four request/response models in ``api/app.py`` be written
without it — so ``/sprint/*`` quietly accepted unknown fields that ``/v1/console/*``
rejected. One import makes the rule hard to forget.
"""

from __future__ import annotations

from pydantic import ConfigDict

STRICT = ConfigDict(extra="forbid")
