// Small reusable bits: icon, copy button, segmented toggle.
import { html, useState, useCallback } from "../lib/preact.js";
import { copyText } from "../lib/format.js";

export function Icon({ path, size = 16, stroke = "currentColor" }) {
    return html`<svg
        width=${size}
        height=${size}
        viewBox="0 0 24 24"
        fill="none"
        stroke=${stroke}
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
    >
        ${path.map((d) => html`<path d=${d} />`)}
    </svg>`;
}

export function CopyButton({ text, label = "copy" }) {
    const [done, setDone] = useState(false);
    const onClick = useCallback(
        async (event) => {
            event.stopPropagation();
            if (await copyText(text)) {
                setDone(true);
                setTimeout(() => setDone(false), 1200);
            }
        },
        [text],
    );
    return html`<button class="copy ${done ? "done" : ""}" onClick=${onClick}>
        ${done ? "copied" : label}
    </button>`;
}

// A row of mutually exclusive buttons; the active one gets the `on` class.
// `options` is a list of `{ value, label }`.
export function Segmented({ options, value, onChange, buttonClass = "dtab" }) {
    return options.map(
        (option) => html`<button
            class="${buttonClass} ${option.value === value ? "on" : ""}"
            onClick=${() => onChange(option.value)}
        >
            ${option.label}
        </button>`,
    );
}
