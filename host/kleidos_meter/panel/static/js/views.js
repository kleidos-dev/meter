// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// @ts-check
/**
 * @file The views, one at a time: the page's fragment (`#live`,
 * `#sequences`, ...) names the one shown, the navigation marks it with
 * `aria-current`, and the others are `hidden`. A view that reads the
 * instrument when it is shown (the relays, the settings, the health, the
 * runs) is told so through its `shown()` hook, and stops a timer of its own
 * through `hidden()`; a hidden view sends nothing.
 */

/**
 * What a view may do when it is shown or hidden.
 *
 * @typedef {object} ViewHooks
 * @property {() => void} [shown] It became the one shown.
 * @property {() => void} [hidden] It stopped being shown.
 */

/**
 * Keep `--masthead-h` on the root at the status band's height, which the
 * stylesheet uses to keep what scrolls into view below the band where it is
 * sticky.
 *
 * @param {Document} page The page.
 */
export function followMasthead(page) {
    const band = page.querySelector(".masthead-band");
    if (!(band instanceof HTMLElement)) {
        return;
    }
    const root = page.documentElement;
    const measure = () => root.style.setProperty("--masthead-h", `${band.offsetHeight}px`);
    new ResizeObserver(measure).observe(band);
    measure();
}

export class Views {
    /**
     * @param {ParentNode} root Where the `[data-view]` sections and the navigation are.
     * @param {string} fallback The view shown for an empty or unknown fragment.
     */
    constructor(root, fallback) {
        /** @type {Map<string, HTMLElement>} */
        this.sections = new Map();
        for (const node of root.querySelectorAll("[data-view]")) {
            const section = /** @type {HTMLElement} */ (node);
            this.sections.set(section.dataset.view ?? "", section);
        }
        this.links = [...root.querySelectorAll(".views a")].map(
            (node) => /** @type {HTMLAnchorElement} */ (node),
        );
        this.fallback = fallback;
        /** @type {Map<string, ViewHooks>} */
        this.hooks = new Map();
        /** @type {string | null} */
        this.current = null;
    }

    /**
     * Be told when a view is shown or hidden.
     *
     * @param {string} name The view.
     * @param {ViewHooks} hooks What to call.
     */
    on(name, hooks) {
        this.hooks.set(name, hooks);
    }

    /** Follow the fragment from now on, and show the view it names. */
    start() {
        followMasthead(document);
        window.addEventListener("hashchange", () => this.show(location.hash.slice(1)));
        this.show(location.hash.slice(1));
    }

    /**
     * Show one view and hide the others.
     *
     * @param {string} name The view's name; an unknown one shows the fallback.
     */
    show(name) {
        const target = this.sections.has(name) ? name : this.fallback;
        if (target === this.current) {
            return;
        }
        const before = this.current;
        this.current = target;
        for (const [key, section] of this.sections) {
            section.hidden = key !== target;
        }
        for (const link of this.links) {
            if (link.hash === `#${target}`) {
                link.setAttribute("aria-current", "page");
            } else {
                link.removeAttribute("aria-current");
            }
        }
        if (before !== null) {
            this.hooks.get(before)?.hidden?.();
        }
        this.hooks.get(target)?.shown?.();
    }

    /**
     * Whether a view is the one shown.
     *
     * @param {string} name The view.
     * @returns {boolean} Whether it is.
     */
    isShown(name) {
        return this.current === name;
    }
}
