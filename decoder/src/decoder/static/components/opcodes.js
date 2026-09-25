// Opcodes view — catalog of opcodes with decode status, size dist & draft schema.
import { html, Fragment, useState, useMemo } from "../lib/preact.js";
import { direction, matchesText, nextOf, sizeRange } from "../lib/format.js";
import { CopyButton } from "./common.js";

const STATUSES = ["mapped", "partial", "unknown"];
const STATUS_FILTERS = ["all", ...STATUSES];
const SORTS = ["frequency", "opcode", "size"];

// A decrypted body starts with its 4-byte opcode; the rest is the payload.
const OPCODE_BYTES = 4;

// unknown = opcode not identified by the catalog; partial = identified but its
// body isn't fully decoded (errors, unresolved fields, or a non-empty payload
// we produced no fields for — an empty payload such as C2S_WaitingUserCount is
// fully decoded with no fields).
function decodeStatus(name, group) {
    if (!name) return "unknown";
    const hasError = group.some((f) => f.error);
    const anyUnresolved = group.some((f) => (f.fields || []).some((x) => x.unresolved));
    const undecodedBody = group.some(
        (f) => f.body_len - OPCODE_BYTES > 0 && (f.fields || []).length === 0,
    );
    return hasError || anyUnresolved || undecodedBody ? "partial" : "mapped";
}

// Group a session's frames by opcode and derive count, size range and a decode
// status from the analysis output.
function opcodeSummary(frames) {
    const groups = new Map();
    for (const frame of frames) {
        if (!groups.has(frame.opcode_hex)) groups.set(frame.opcode_hex, []);
        groups.get(frame.opcode_hex).push(frame);
    }
    return Array.from(groups, ([opcode, group]) => {
        const lens = group.map((f) => f.body_len);
        const name = (group.find((f) => f.name) || {}).name || "";
        return {
            opcode,
            name,
            direction: group[0].direction,
            count: group.length,
            min: Math.min(...lens),
            max: Math.max(...lens),
            status: decodeStatus(name, group),
            frames: group,
        };
    });
}

const SORTERS = {
    frequency: (a, b) => b.count - a.count,
    opcode: (a, b) => a.opcode.localeCompare(b.opcode),
    size: (a, b) => b.max - a.max,
};

function sizeHistogram(frames, bucketCount = 8) {
    const lens = frames.map((f) => f.body_len);
    const min = Math.min(...lens);
    const max = Math.max(...lens);
    if (min === max)
        return { bars: [{ label: `${min}B`, count: lens.length }], max: lens.length };
    const width = Math.ceil((max - min + 1) / bucketCount) || 1;
    const buckets = [];
    for (let start = min; start <= max; start += width)
        buckets.push({ from: start, count: 0 });
    for (const len of lens) {
        const idx = Math.min(buckets.length - 1, Math.floor((len - min) / width));
        buckets[idx].count += 1;
    }
    const peak = Math.max(...buckets.map((b) => b.count));
    return {
        bars: buckets.map((b) => ({ label: `${b.from}B`, count: b.count })),
        max: peak,
    };
}

// A YAML draft built from the richest decoded frame of an opcode.
function schemaYaml(entry) {
    const rep = entry.frames.reduce(
        (best, f) =>
            (f.fields || []).length > (best.fields || []).length ? f : best,
        entry.frames[0],
    );
    const lines = [
        `opcode: ${entry.opcode}`,
        `name: ${entry.name || "UNKNOWN"}`,
        `direction: ${entry.direction === "C2S" ? "c2s" : "s2c"}`,
        `size: ${sizeRange(entry.min, entry.max, "-")}`,
        "fields:",
    ];
    const fields = rep.fields || [];
    if (!fields.length) {
        lines.push("  []  # body not decoded");
    } else {
        for (const f of fields)
            lines.push(
                `  - { type: ${f.type}, name: ${f.name}${f.unresolved ? "  # unresolved" : ""} }`,
            );
    }
    return lines.join("\n") + "\n";
}

function StatusPill({ status }) {
    return html`<span class="pill st-${status}">${status}</span>`;
}

function SizeHistogram({ hist }) {
    const barWidth = 540 / hist.bars.length;
    return html`<${Fragment}>
        <svg class="hist-svg" viewBox="0 0 540 112" preserveAspectRatio="none">
            ${hist.bars.map((bar, i) => {
                const height = hist.max
                    ? Math.max((bar.count / hist.max) * 100, bar.count ? 2 : 0)
                    : 0;
                return html`<rect
                    x=${(i * barWidth + 4).toFixed(1)}
                    y=${(110 - height).toFixed(1)}
                    width=${(barWidth - 8).toFixed(1)}
                    height=${height.toFixed(1)}
                />`;
            })}
            <line x1="0" y1="110.5" x2="540" y2="110.5" stroke-width="1" />
        </svg>
        <div class="hist-labels">
            ${hist.bars.map((bar) => html`<span>${bar.label}</span>`)}
        </div>
    </${Fragment}>`;
}

function OpcodeDetail({ entry }) {
    const hist = useMemo(() => sizeHistogram(entry.frames), [entry]);
    const yaml = useMemo(() => schemaYaml(entry), [entry]);
    const dir = direction(entry.direction);

    return html`<aside class="ops-detail">
        <div class="ops-detail-head">
            <div>
                <div class="ops-detail-title">
                    ${entry.opcode}${" "}
                    <span class=${entry.status === "unknown" ? "name-unknown" : "name-known"}
                        >${entry.name || "unknown"}</span
                    >
                </div>
                <div class="ops-detail-sub">
                    <span class=${dir.cls}>${dir.label}</span>
                    · ${entry.count.toLocaleString()} occurrences ·
                    ${sizeRange(entry.min, entry.max)} bytes
                </div>
            </div>
        </div>

        <div class="card">
            <span class="lbl">Size distribution</span>
            <${SizeHistogram} hist=${hist} />
        </div>

        <div class="card fill">
            <div class="chart-head">
                <span class="lbl">Schema (draft)</span>
                <${CopyButton} text=${yaml} label="copy YAML" />
            </div>
            <pre class="schema-pre">${yaml}</pre>
            <span class="ops-detail-sub note">
                Drafted from the richest decoded packet — types come from the
                runtime dump; byte offsets aren't emitted yet.
            </span>
        </div>
    </aside>`;
}

export function OpcodesView({ session }) {
    const [query, setQuery] = useState("");
    const [statusFilter, setStatusFilter] = useState("all");
    const [sort, setSort] = useState("frequency");
    const [selected, setSelected] = useState(null);

    const all = useMemo(() => opcodeSummary(session.frames || []), [session]);

    const counts = useMemo(() => {
        const c = Object.fromEntries(STATUSES.map((status) => [status, 0]));
        for (const e of all) c[e.status] += 1;
        return c;
    }, [all]);

    const rows = useMemo(
        () =>
            all
                .filter(
                    (e) =>
                        (statusFilter === "all" || e.status === statusFilter) &&
                        matchesText(query, e.opcode, e.name),
                )
                .sort(SORTERS[sort]),
        [all, query, statusFilter, sort],
    );

    const active = rows.find((e) => e.opcode === selected) || rows[0] || null;

    return html`<div class="ops">
        <section class="ops-list">
            <div class="ops-toolbar">
                <input
                    class="in"
                    placeholder="Filter by opcode or name"
                    value=${query}
                    onInput=${(e) => setQuery(e.target.value)}
                />
                <button
                    class="btn"
                    onClick=${() => setStatusFilter(nextOf(STATUS_FILTERS, statusFilter))}
                >
                    Status: ${statusFilter}
                </button>
                <button class="btn" onClick=${() => setSort(nextOf(SORTS, sort))}>
                    Sort: ${sort}
                </button>
                <div class="counts">
                    <span>${all.length} opcodes</span>
                    ${STATUSES.map(
                        (status) =>
                            html`<span>·</span
                                ><span class="st-${status}">${counts[status]} ${status}</span>`,
                    )}
                </div>
            </div>
            <div class="orow colhdr">
                <span>Opcode</span><span>Name</span><span>Dir</span
                ><span class="ocount">Count</span><span>Size</span><span>Status</span>
            </div>
            <div class="ops-body">
                ${rows.length
                    ? rows.map((e) => {
                          const dir = direction(e.direction);
                          return html`<div
                              class="orow ${active && active.opcode === e.opcode ? "sel" : ""}"
                              onClick=${() => setSelected(e.opcode)}
                          >
                              <span>${e.opcode}</span>
                              <span class="oname ${e.status === "unknown" ? "unk" : ""}"
                                  >${e.name || "—"}</span
                              >
                              <span class=${dir.cls}>${dir.label}</span>
                              <span class="ocount">${e.count.toLocaleString()}</span>
                              <span class="osize">${sizeRange(e.min, e.max)}</span>
                              <${StatusPill} status=${e.status} />
                          </div>`;
                      })
                    : html`<div class="list-empty">No opcodes match the filter.</div>`}
            </div>
        </section>

        ${active ? html`<${OpcodeDetail} entry=${active} />` : null}
    </div>`;
}
