(function (root) {
    'use strict';

    const HTML_ESCAPES = Object.freeze({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    });

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, character => HTML_ESCAPES[character]);
    }

    function escapeAttr(value) {
        return escapeHtml(value);
    }

    function safeHttpUrl(value) {
        if (typeof value !== 'string' || !value.trim()) return '';
        try {
            const parsed = new URL(value.trim());
            if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
            return parsed.href;
        } catch (_) {
            return '';
        }
    }

    const api = Object.freeze({escapeHtml, escapeAttr, safeHttpUrl});
    root.MoneyManiSafe = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
