"""Content-marking strategies.

The content scorer is one subprocess that marks one consultation against its
rubric. How many of them run, against which models, and what happens to their
sheets afterwards is a *strategy*:

* :mod:`single` — one marker, one sheet. Today's behaviour and the default.
* ``panel`` (later) — several markers in parallel and an adjudicator for the
  criteria they disagree on.

:mod:`base` holds what every strategy shares: the :class:`MarkingPlan` the
settings service resolves once per run, and the :class:`ContentMarkerRunner`
that spawns one marker.
"""
