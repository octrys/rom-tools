// Pure helpers shared by the views: formatting, frame predicates, text match.

const DIRECTIONS = {
    C2S: { label: "C→S", arrow: "↑", cls: "dc" },
    S2C: { label: "S→C", arrow: "↓", cls: "ds" },
};
const MIXED_DIRECTION = { label: "↔", arrow: "↕", cls: "db" };

// Display label, list arrow and color class for a frame direction.
export function direction(value) {
    return DIRECTIONS[value] || MIXED_DIRECTION;
}

// Uppercase hex, zero-padded to at least `width` digits.
export function toHex(value, width) {
    return value.toString(16).padStart(width, "0").toUpperCase();
}

// "12" for a single size, "12–40" for a range.
export function sizeRange(min, max, separator = "–") {
    return min === max ? `${min}` : `${min}${separator}${max}`;
}

export function hexToBytes(hexStr) {
    if (!hexStr) return new Uint8Array(0);
    const bytes = new Uint8Array(Math.floor(hexStr.length / 2));
    for (let i = 0; i + 1 < hexStr.length; i += 2)
        bytes[i / 2] = parseInt(hexStr.slice(i, i + 2), 16);
    return bytes;
}

export function isContainer(value) {
    return value !== null && typeof value === "object";
}

export function isUnknownFrame(frame) {
    return Boolean(frame.error) || !frame.name;
}

// Case-insensitive substring match of `needle` against the joined `parts`.
// An empty needle matches everything.
export function matchesText(needle, ...parts) {
    const normalized = needle.trim().toLowerCase();
    if (!normalized) return true;
    return parts.join(" ").toLowerCase().includes(normalized);
}

// The element after `current` in `list`, wrapping around.
export function nextOf(list, current) {
    return list[(list.indexOf(current) + 1) % list.length];
}

export async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (err) {
        console.warn("clipboard write failed:", err);
        return false;
    }
}
