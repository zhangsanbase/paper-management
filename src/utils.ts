import type { TagItem } from "./types";

export function splitTextList(value: string): string[] {
  return value
    .replace(/[,，]/g, "；")
    .split("；")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function tagMatchesQuery(tag: TagItem, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [tag.name, tag.description, ...tag.aliases]
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
    .includes(needle);
}
