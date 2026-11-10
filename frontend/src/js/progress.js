function nonNegativeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : 0;
}

export function calculatePageProgress(completedPages, totalPages) {
  const total = nonNegativeInteger(totalPages);
  const completed = Math.min(total, nonNegativeInteger(completedPages));
  return {
    completed_pages: completed,
    total_pages: total,
    percentage: total > 0 ? Math.round((completed / total) * 100) : null,
  };
}

export function getProgressPercentage(progress) {
  if (progress?.status === "completed") return 100;
  const reported = Number(progress?.progress_percent);
  if (Number.isFinite(reported)) return Math.max(0, Math.min(100, Math.floor(reported)));
  return calculatePageProgress(progress?.completed_pages, progress?.total_pages).percentage ?? 0;
}

export function aggregatePageProgress(progressItems) {
  const items = Array.isArray(progressItems) ? progressItems : [];
  const countsKnown = items.length > 0 && items.every((item) => nonNegativeInteger(item?.total_pages) > 0);
  const completedPages = items.reduce((sum, item) =>
    sum + calculatePageProgress(item?.completed_pages, item?.total_pages).completed_pages, 0);
  const totalPages = items.reduce((sum, item) => sum + nonNegativeInteger(item?.total_pages), 0);
  const hasStageProgress = items.some((item) =>
    item?.status === "completed" || Number.isFinite(Number(item?.progress_percent))
  );
  const percentage = hasStageProgress && totalPages > 0
    ? Math.round(items.reduce((sum, item) => {
      const pageCount = nonNegativeInteger(item?.total_pages);
      const reported = item?.status === "completed" ? 100 : Number(item?.progress_percent);
      const pagePercent = Number.isFinite(reported)
        ? Math.max(0, Math.min(100, reported))
        : calculatePageProgress(item?.completed_pages, pageCount).percentage ?? 0;
      return sum + pagePercent * pageCount;
    }, 0) / totalPages)
    : countsKnown && totalPages > 0
      ? Math.round((completedPages / totalPages) * 100)
      : null;
  return {
    counts_known: countsKnown,
    completed_pages: completedPages,
    total_pages: countsKnown ? totalPages : null,
    percentage,
    progress_mode: hasStageProgress ? "processing_work" : "completed_pages",
  };
}
