// Single-flight wrapper for a refresh that many signals may ask for at once.
//
// The change stream announces every committed write, and a run writes often:
// a progress reading every couple of percent, each step transition, the job
// row mirrored onto the session — and under PARALLEL_SCORING two branches do
// this at the same time, on PostgreSQL each as its own pg_notify. Refetching
// the session index for every one of those made two things go wrong:
//
//   * a burst of N events cost N concurrent GET /api/sessions, each rebuilding
//     an index the previous one had just evicted;
//   * two of those requests could complete out of order, so a slower, older
//     response replaced a newer one and the cards showed a step the run had
//     already left — until the next write happened to fix it.
//
// `coalesceAsync` turns any number of triggers into "one run now, and at most
// one more afterwards if anything asked while it was in flight". Responses can
// no longer overlap, so they cannot land out of order, and a burst collapses
// to two requests: the one that was running and one that catches up.
//
// Framework-free so the contract is pinned by plain `node --test`
// (see test/coalesce.test.mjs).

/**
 * Wrap `run` so concurrent triggers share one in-flight execution.
 *
 * Returns a trigger. Calling it while a run is in flight marks a re-run and
 * resolves with that in-flight promise; when the run settles, exactly one more
 * begins if anything asked in the meantime. A rejection from `run` reaches the
 * callers that were handed that promise; a rejected catch-up run is swallowed,
 * because nobody is left holding it.
 */
export function coalesceAsync(run) {
  let inFlight = null;
  let rerunWanted = false;

  const start = () => {
    inFlight = (async () => {
      try {
        return await run();
      } finally {
        inFlight = null;
        if (rerunWanted) {
          rerunWanted = false;
          start().catch(() => {});
        }
      }
    })();
    return inFlight;
  };

  return function trigger() {
    if (inFlight) {
      rerunWanted = true;
      return inFlight;
    }
    return start();
  };
}
