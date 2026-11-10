function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  const minutes = String(Math.floor(totalSeconds / 60) % 60).padStart(2, "0");
  const hours = Math.floor(totalSeconds / 3600);
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${minutes}:${seconds}`
    : `${minutes}:${seconds}`;
}

/** Show elapsed wall-clock time without changing reported extraction progress. */
export function createElapsedTimer(element) {
  let startedAt = null;
  let stoppedAt = null;
  let intervalId = null;

  const render = () => {
    if (!element) return;
    const elapsed = startedAt === null ? 0 : (stoppedAt ?? Date.now()) - startedAt;
    element.textContent = `Durasi ${formatElapsed(elapsed)}`;
  };
  const clearInterval = () => {
    if (intervalId !== null) window.clearInterval(intervalId);
    intervalId = null;
  };

  render();
  return {
    start() {
      if (startedAt !== null) return;
      startedAt = Date.now();
      render();
      intervalId = window.setInterval(render, 1000);
    },
    stop() {
      if (startedAt === null || stoppedAt !== null) return;
      stoppedAt = Date.now();
      clearInterval();
      render();
    },
    reset() {
      clearInterval();
      startedAt = null;
      stoppedAt = null;
      render();
    },
  };
}
