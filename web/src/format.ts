/** Metric keys that are fractions in [0, 1]: always shown with two decimals, even when 0 or 1. */
const FRACTION_KEY = /(coverage|ratio|rate|fraction|share)$/i;

/** Human-readable metric: fractions to 2 decimals, counts as integers, never a raw float. */
export function formatMetric(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  if (!Number.isFinite(value)) return "—";
  return Number.isInteger(value) && !FRACTION_KEY.test(key) ? String(value) : value.toFixed(2);
}
