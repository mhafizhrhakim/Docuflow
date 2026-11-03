/** Run independent work with a fixed upper bound and keep results in input order. */
export async function runBounded(items, limit, task) {
  const values = [...items];
  const workerCount = Math.min(values.length, Math.max(1, Math.floor(limit) || 1));
  const results = new Array(values.length);
  let next = 0;

  await Promise.all(Array.from({ length: workerCount }, async () => {
    while (true) {
      const index = next;
      next += 1;
      if (index >= values.length) return;
      try {
        results[index] = { status: "fulfilled", value: await task(values[index], index) };
      } catch (reason) {
        results[index] = { status: "rejected", reason };
      }
    }
  }));

  return results;
}
