// Texts come from web/i18n.py. Placeholders like {count} are filled in as in Python's
// str.format; an unknown key is shown as it is, as on the server.

export type Translate = (key: string, values?: Record<string, string | number>) => string;

export function makeTranslate(texts: Record<string, string>): Translate {
  return (key, values) => {
    const text = texts[key];
    if (text === undefined) return key;
    if (!values) return text;
    return text.replace(/\{(\w+)\}/g, (match, name: string) =>
      name in values ? String(values[name]) : match,
    );
  };
}

export const ROLE_ORDER = ["viewer", "operator", "configurator", "admin"];

export function hasRole(role: string, needed: string): boolean {
  return ROLE_ORDER.indexOf(role) >= ROLE_ORDER.indexOf(needed);
}
