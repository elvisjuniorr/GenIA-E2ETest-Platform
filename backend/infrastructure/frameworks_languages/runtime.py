"""Runtime and translation helpers for framework/language execution strategies."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from textwrap import dedent


def discover_node_executable() -> str | None:
    candidates = [
        Path(__file__).resolve().parents[2] / "venv" / "Lib" / "site-packages" / "playwright" / "driver" / "node.exe",
        Path(__file__).resolve().parents[2] / "venv" / "Lib" / "site-packages" / "patchright" / "driver" / "node.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def discover_playwright_package_dir() -> str | None:
    candidates = [
        Path(__file__).resolve().parents[2] / "venv" / "Lib" / "site-packages" / "playwright" / "driver" / "package",
        Path(__file__).resolve().parents[2] / "venv" / "Lib" / "site-packages" / "patchright" / "driver" / "package",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def discover_chrome_executable() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\new_chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def strip_code_fences(source: str) -> str:
    cleaned = source.replace("\ufeff", "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned


def strip_typescript_annotations(source: str) -> str:
    cleaned = source
    cleaned = re.sub(r":\s*[A-Za-z_][A-Za-z0-9_<>,\[\]\| ]*(?=[=,;\)\{])", "", cleaned)
    cleaned = re.sub(r"\bas\s+[A-Za-z_][A-Za-z0-9_<>,\[\]\| ]*", "", cleaned)
    cleaned = re.sub(r"\binterface\s+\w+\s*\{.*?\}", "", cleaned, flags=re.S)
    cleaned = re.sub(r"\btype\s+\w+\s*=\s*.*?;", "", cleaned, flags=re.S)
    return cleaned


def strip_common_imports(source: str) -> str:
    cleaned_lines: list[str] = []
    for line in source.splitlines():
        stripped = line.replace("\ufeff", "").strip()
        if not stripped:
            cleaned_lines.append(line)
            continue
        if stripped.startswith("import "):
            continue
        if "require('@playwright/test')" in stripped or 'require("@playwright/test")' in stripped:
            continue
        if "require('selenium-webdriver')" in stripped or 'require("selenium-webdriver")' in stripped:
            continue
        if "require('selenium-webdriver/chrome')" in stripped or 'require("selenium-webdriver/chrome")' in stripped:
            continue
        if "require('selenium-webdriver/firefox')" in stripped or 'require("selenium-webdriver/firefox")' in stripped:
            continue
        if "require('cypress')" in stripped or 'require("cypress")' in stripped:
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"^\s*const\s*\{[^}]*\}\s*=\s*require\([^\n]+\)\s*;\s*$", "", cleaned, flags=re.M)
    return cleaned


def _extract_block_after_keyword(source: str, keyword: str) -> str | None:
    pattern = re.compile(re.escape(keyword) + r"\s*\(", re.M)
    match = pattern.search(source)
    if not match:
        return None
    start = source.find("{", match.end())
    if start < 0:
        return None
    depth = 0
    for idx in range(start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : idx]
    return None


def extract_first_callback_body(source: str, keywords: tuple[str, ...]) -> str | None:
    for keyword in keywords:
        body = _extract_block_after_keyword(source, keyword)
        if body:
            return body
    return None


def extract_async_iife_body(source: str) -> str | None:
    patterns = [
        r"\(\s*async\s*\(\s*\)\s*=>\s*\{",
        r"\(\s*async\s+function\s*\(\s*\)\s*\{",
    ]
    for pattern in patterns:
        match = re.search(pattern, source)
        if not match:
            continue
        start = source.find("{", match.end() - 1)
        if start < 0:
            continue
        depth = 0
        for idx in range(start, len(source)):
            ch = source[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return source[start + 1 : idx]
    return None


def extract_java_methods(source: str) -> list[tuple[str, str, str]]:
    cleaned = source.replace("\r\n", "\n")
    methods: list[tuple[str, str, str]] = []
    lines = cleaned.splitlines()
    annotations: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if stripped.startswith("@"):
            annotations.append(stripped)
            idx += 1
            continue
        signature_match = re.match(
            r"(?:public|private|protected)?\s*(?:static\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\([^)]*\)\s*\{",
            stripped,
        )
        if signature_match:
            method_name = signature_match.group(1)
            body_lines = []
            brace_depth = 0
            started = False
            while idx < len(lines):
                current = lines[idx]
                body_lines.append(current)
                brace_depth += current.count("{")
                brace_depth -= current.count("}")
                if "{" in current:
                    started = True
                idx += 1
                if started and brace_depth == 0:
                    break
            body = "\n".join(body_lines)
            methods.append((" ".join(annotations), method_name, body))
            annotations = []
            continue
        annotations = []
        idx += 1
    return methods


def parse_java_method_signature(method_source: str) -> tuple[str, list[str], bool]:
    signature_line = ""
    for line in method_source.splitlines():
        stripped = line.strip()
        if stripped:
            signature_line = stripped.rstrip("{").strip()
            break

    match = re.match(
        r"(?:(?:public|private|protected)\s+)?(?:(?:static)\s+)?(?:(?P<ret>[\w<>\[\], ?]+?)\s+)?(?P<name>\w+)\s*\((?P<params>[^)]*)\)",
        signature_line,
    )
    if not match:
        return "anonymous", [], False

    raw_params = match.group("params") or ""
    params: list[str] = []
    for raw_param in [part.strip() for part in raw_params.split(",") if part.strip()]:
        cleaned_param = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", raw_param)
        cleaned_param = re.sub(r"\bfinal\s+", "", cleaned_param)
        tokenized = cleaned_param.replace("...", " ").split()
        if tokenized:
            params.append(tokenized[-1].replace("...", "").strip())

    return match.group("name"), params, match.group("ret") is None


def extract_java_method_body(method_source: str) -> str:
    start = method_source.find("{")
    end = method_source.rfind("}")
    if start >= 0 and end > start:
        return method_source[start + 1 : end]
    return method_source


def translate_java_method_to_js(method_source: str, helper_names: list[str] | None = None) -> str:
    name, params, _ = parse_java_method_signature(method_source)
    body = extract_java_method_body(method_source)
    translated = translate_java_body_to_js(body)
    translated = re.sub(r"\bthis\.", "", translated)
    translated = await_java_helper_calls(translated, helper_names or [])
    translated = translated.strip()
    params_text = ", ".join(params)
    return f"async function {name}({params_text}) {{\n{translated}\n}}"


def await_java_helper_calls(source: str, helper_names: list[str]) -> str:
    if not helper_names:
        return source
    pattern = r"(?<!await\s)(?<!\.)\b(" + "|".join(re.escape(name) for name in helper_names) + r")\s*\("
    return re.sub(pattern, r"await \1(", source)


def translate_java_source_to_js(source: str) -> str:
    cleaned = strip_code_fences(source)
    cleaned = strip_common_imports(cleaned)
    methods = extract_java_methods(cleaned)
    helper_names = [parse_java_method_signature(method_source)[0] for annotations, _method_name, method_source in methods if not annotations.strip()]
    helper_defs: list[str] = []
    executable_blocks: list[str] = []

    for annotations, _method_name, method_source in methods:
        method_name, _, _ = parse_java_method_signature(method_source)
        if annotations.strip():
            block = translate_java_body_to_js(extract_java_method_body(method_source)).strip()
            executable_blocks.append(block)
        else:
            translated_method = translate_java_method_to_js(method_source, helper_names)
            helper_defs.append(translated_method)

    executable_blocks = [await_java_helper_calls(block, helper_names) for block in executable_blocks]

    if helper_defs and executable_blocks:
        return "\n\n".join([*helper_defs, *executable_blocks])
    if helper_defs:
        return "\n\n".join(helper_defs)
    if executable_blocks:
        return "\n\n".join(executable_blocks)
    return translate_java_body_to_js(cleaned)


def translate_java_body_to_js(body: str) -> str:
    translated = body.replace("\r\n", "\n")
    translated = translated.replace("System.out.println", "console.log")
    translated = re.sub(r"\bThread\.sleep\s*\(\s*(\d+)\s*\)", r"await sleep(\1)", translated)
    translated = re.sub(r"\bDuration\.ofSeconds\s*\(\s*(\d+)\s*\)", r"(\1 * 1000)", translated)
    translated = re.sub(r"\bnew\s+ChromeDriver\s*\(\s*\)", "await createDriver('chrome')", translated)
    translated = re.sub(r"\bnew\s+FirefoxDriver\s*\(\s*\)", "await createDriver('firefox')", translated)
    translated = re.sub(r"\bnew\s+EdgeDriver\s*\(\s*\)", "await createDriver('edge')", translated)
    translated = re.sub(r"\bnew\s+SafariDriver\s*\(\s*\)", "await createDriver('webkit')", translated)
    translated = re.sub(r"\bnew\s+WebDriverWait\s*\(\s*driver\s*,\s*([^)]+)\)", r"new WebDriverWait(driver, \1)", translated)
    translated = re.sub(r"\bAssertions\.assertTrue\s*\(", "assertTrue(", translated)
    translated = re.sub(r"\bAssertions\.assertFalse\s*\(", "assertFalse(", translated)
    translated = re.sub(r"\bAssertions\.assertEquals\s*\(", "assertEquals(", translated)
    translated = re.sub(r"\bAssertions\.assertNotNull\s*\(", "assertNotNull(", translated)
    translated = re.sub(r"\bAssertions\.assertNull\s*\(", "assertNull(", translated)
    translated = re.sub(r"\bassertTrue\s*\(", "assertTrue(", translated)
    translated = re.sub(r"\bassertFalse\s*\(", "assertFalse(", translated)
    translated = re.sub(r"\bassertEquals\s*\(", "assertEquals(", translated)
    translated = re.sub(r"\bassertNotNull\s*\(", "assertNotNull(", translated)
    translated = re.sub(r"\bassertNull\s*\(", "assertNull(", translated)
    translated = re.sub(r"\b(?:WebDriver|WebElement|WebDriverWait|String|int|boolean|double|float|long|List<[^>]+>|Map<[^>]+>|Set<[^>]+>)\s+([A-Za-z_]\w*)\s*=", r"let \1 =", translated)
    translated = re.sub(r"\bfinal\s+(?:WebDriver|WebElement|WebDriverWait|String|int|boolean|double|float|long|List<[^>]+>|Map<[^>]+>|Set<[^>]+>)\s+([A-Za-z_]\w*)\s*=", r"const \1 =", translated)
    translated = re.sub(r"\bnew\s+WebDriverWait\s*\(\s*driver\s*,\s*([^)]+)\s*\)", r"new WebDriverWait(driver, \1)", translated)
    translated = re.sub(r"\bExpectedConditions\.", "ExpectedConditions.", translated)
    translated = translated.replace("driver.quit();", "await driver.quit();")
    translated = re.sub(r"\bdriver\.get\s*\(", "await driver.get(", translated)
    translated = re.sub(r"\bdriver\.navigate\(\)\.to\s*\(", "await driver.get(", translated)
    translated = re.sub(r"\bdriver\.findElement\(", "await driver.findElement(", translated)
    translated = re.sub(r"\bdriver\.findElements\(", "await driver.findElements(", translated)
    translated = re.sub(r"\b(\w+)\.until\s*\(", r"await \1.until(", translated)
    translated = re.sub(r"(?<!await\s)(\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.(click|clear|sendKeys|submit|check|uncheck|focus|blur)\s*\(", r"await \1.\2(", translated)
    translated = re.sub(r"\bdriver\.getTitle\s*\(\s*\)", "(await driver.getTitle())", translated)
    translated = re.sub(r"\bdriver\.getCurrentUrl\s*\(\s*\)", "(await driver.getCurrentUrl())", translated)
    translated = re.sub(r"(\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.includes\s*\(", r"String(\1).includes(", translated)
    translated = re.sub(r"(\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.contains\s*\(", r"String(\1).includes(", translated)
    translated = re.sub(r"\.sendKeys\s*\(", ".sendKeys(", translated)
    translated = re.sub(r"\.click\s*\(\s*\)", ".click()", translated)
    translated = translated.replace("new WebDriverWait(driver, ", "new WebDriverWait(driver, ")
    translated = re.sub(r"\bpublic\s+", "", translated)
    translated = re.sub(r"\bprivate\s+", "", translated)
    translated = re.sub(r"\bprotected\s+", "", translated)
    translated = re.sub(r"\bstatic\s+", "", translated)
    translated = re.sub(r"\bvoid\s+", "", translated)
    translated = re.sub(r"\bthrows\s+[A-Za-z0-9_, ]+", "", translated)
    translated = re.sub(r"^\s*@\w+(?:\([^)]*\))?\s*$", "", translated, flags=re.M)
    return translated


def build_js_runtime(source_body: str, *, mode: str, browser_headless: bool = True, use_all_shims: bool = True) -> str:
    playwright_dir = discover_playwright_package_dir()
    node_pkg = json.dumps(playwright_dir or "")
    chrome_path = json.dumps(discover_chrome_executable() or "")
    body = source_body
    return dedent(
        f"""
        const path = require('path');
        const assert = require('assert');
        const pw = require({node_pkg});
        const {{ chromium }} = pw;
        const CHROME_PATH = {chrome_path};
        const HEADLESS = {str(browser_headless).lower()};
        const VISUALIZE_EXECUTION = process.env.GENIA_VISUALIZE_EXECUTION === '1';
        let visualFrameCount = 0;
        let visualFrameBusy = false;
        const VISUAL_ACTION_LABELS = {{
          goto: 'Abrindo página',
          reload: 'Recarregando página',
          goBack: 'Voltando página',
          goForward: 'Avançando página',
          click: 'Clicando em elemento',
          dblclick: 'Clicando duas vezes em elemento',
          fill: 'Preenchendo campo',
          type: 'Digitando em campo',
          selectOption: 'Selecionando opção',
          check: 'Marcando opção',
          uncheck: 'Desmarcando opção',
          press: 'Interagindo com campo',
        }};

        async function emitVisualFrame(page, label) {{
          if (!VISUALIZE_EXECUTION || visualFrameCount >= 60 || visualFrameBusy) return;
          visualFrameBusy = true;
          try {{
            const image = await page.screenshot({{ type: 'jpeg', quality: 38, animations: 'disabled' }});
            let url = '';
            try {{
              const current = new URL(page.url());
              url = current.origin + current.pathname;
            }} catch (_) {{}}
            const frame = {{ image: image.toString('base64'), label, url }};
            process.stdout.write('\\n__GENIA_VISUAL__' + JSON.stringify(frame) + '\\n');
            visualFrameCount += 1;
          }} catch (_) {{
            // A screenshot is optional feedback and must never fail the test.
          }} finally {{
            visualFrameBusy = false;
          }}
        }}

        function wrapVisualLocator(locator, page) {{
          if (!VISUALIZE_EXECUTION || !locator) return locator;
          return new Proxy(locator, {{
            get(target, property) {{
              const member = Reflect.get(target, property, target);
              if (typeof member !== 'function') return member;
              const label = VISUAL_ACTION_LABELS[String(property)];
              if (label) return async (...args) => {{
                const result = await member.apply(target, args);
                await emitVisualFrame(page, label);
                return result;
              }};
              if (['first', 'last', 'nth', 'locator', 'filter'].includes(String(property))) {{
                return (...args) => wrapVisualLocator(member.apply(target, args), page);
              }}
              return member.bind(target);
            }},
          }});
        }}

        function enableVisualFeedback(page) {{
          if (!VISUALIZE_EXECUTION) return;
          for (const name of ['goto', 'reload', 'goBack', 'goForward']) {{
            const original = page[name]?.bind(page);
            if (!original) continue;
            page[name] = async (...args) => {{
              const result = await original(...args);
              await emitVisualFrame(page, VISUAL_ACTION_LABELS[name]);
              return result;
            }};
          }}
          for (const name of ['locator', 'getByRole', 'getByText', 'getByLabel', 'getByPlaceholder', 'getByTestId', 'getByAltText', 'getByTitle']) {{
            const original = page[name]?.bind(page);
            if (original) page[name] = (...args) => wrapVisualLocator(original(...args), page);
          }}
        }}

        function isLocator(value) {{
          return value && typeof value.click === 'function' && typeof value.waitFor === 'function';
        }}

        function normalizeText(value) {{
          if (value === null || value === undefined) return '';
          return String(value);
        }}

        function simpleExpect(actual) {{
          const api = {{
            async toBeVisible(options = {{}}) {{
              if (isLocator(actual)) {{
                await actual.waitFor({{ state: 'visible', timeout: options.timeout || 5000 }});
                return;
              }}
              assert.ok(!!actual, 'Expected value to be truthy and visible');
            }},
            async toBeHidden() {{
              if (isLocator(actual)) {{
                await actual.waitFor({{ state: 'hidden', timeout: 5000 }});
                return;
              }}
              assert.ok(!actual, 'Expected value to be hidden/falsy');
            }},
            async toBeEnabled() {{
              if (isLocator(actual)) {{
                assert.ok(await actual.isEnabled(), 'Expected element to be enabled');
                return;
              }}
              assert.ok(!!actual, 'Expected value to be enabled/truthy');
            }},
            async toBeDisabled() {{
              if (isLocator(actual)) {{
                assert.ok(!(await actual.isEnabled()), 'Expected element to be disabled');
                return;
              }}
              assert.ok(!actual, 'Expected value to be disabled/falsy');
            }},
            async toBeChecked() {{
              if (isLocator(actual)) {{
                assert.ok(await actual.isChecked(), 'Expected element to be checked');
                return;
              }}
              assert.ok(!!actual, 'Expected value to be checked/truthy');
            }},
            async toHaveText(expected) {{
              const text = isLocator(actual) ? normalizeText(await actual.textContent()) : normalizeText(actual);
              if (expected instanceof RegExp) {{
                assert.ok(expected.test(text), `Expected text to match ${{expected}} but got ${{text}}`);
              }} else {{
                assert.strictEqual(text.trim(), normalizeText(expected).trim());
              }}
            }},
            async toContainText(expected) {{
              const text = isLocator(actual) ? normalizeText(await actual.textContent()) : normalizeText(actual);
              if (expected instanceof RegExp) {{
                assert.ok(expected.test(text), `Expected text to match ${{expected}} but got ${{text}}`);
              }} else {{
                assert.ok(text.includes(normalizeText(expected)), `Expected text to contain ${{expected}} but got ${{text}}`);
              }}
            }},
            async toHaveValue(expected) {{
              const value = isLocator(actual) ? normalizeText(await actual.inputValue()) : normalizeText(actual);
              assert.strictEqual(value, normalizeText(expected));
            }},
            async toHaveCount(expected) {{
              if (isLocator(actual)) {{
                assert.strictEqual(await actual.count(), expected);
                return;
              }}
              assert.strictEqual(Array.isArray(actual) ? actual.length : 0, expected);
            }},
            async toHaveURL(expected) {{
              const url = actual && typeof actual.url === 'function' ? actual.url() : normalizeText(actual);
              if (expected instanceof RegExp) {{
                assert.ok(expected.test(url), `Expected URL to match ${{expected}} but got ${{url}}`);
              }} else {{
                assert.ok(url.includes(normalizeText(expected)), `Expected URL to contain ${{expected}} but got ${{url}}`);
              }}
            }},
            async toHaveTitle(expected) {{
              const title = actual && typeof actual.title === 'function' ? await actual.title() : normalizeText(actual);
              if (expected instanceof RegExp) {{
                assert.ok(expected.test(title), `Expected title to match ${{expected}} but got ${{title}}`);
              }} else {{
                assert.ok(title.includes(normalizeText(expected)), `Expected title to contain ${{expected}} but got ${{title}}`);
              }}
            }},
            toBeTruthy() {{
              assert.ok(!!actual, 'Expected value to be truthy');
            }},
            toBeFalsy() {{
              assert.ok(!actual, 'Expected value to be falsy');
            }},
            toEqual(expected) {{
              assert.deepStrictEqual(actual, expected);
            }},
            toMatch(expected) {{
              const value = normalizeText(actual);
              if (expected instanceof RegExp) {{
                assert.ok(expected.test(value), `Expected value to match ${{expected}} but got ${{value}}`);
              }} else {{
                assert.ok(value.includes(normalizeText(expected)), `Expected value to contain ${{expected}} but got ${{value}}`);
              }}
            }},
          }};
          api.not = {{
            toBeVisible: async () => {{
              if (isLocator(actual)) {{
                const visible = await actual.isVisible().catch(() => false);
                assert.ok(!visible, 'Expected element to not be visible');
                return;
              }}
              assert.ok(!actual, 'Expected value to be falsy');
            }},
            toEqual: (expected) => {{
              assert.notDeepStrictEqual(actual, expected);
            }},
          }};
          return api;
        }}

        async function createBrowser() {{
          const browser = await chromium.launch({{
            headless: HEADLESS,
            executablePath: CHROME_PATH || undefined,
          }});
          const context = await browser.newContext();
          const page = await context.newPage();
          return {{ browser, context, page }};
        }}

        function locatorFrom(page, selector, strategy) {{
          if (strategy === 'xpath') return page.locator(`xpath=${{selector}}`);
          if (strategy === 'css') return page.locator(selector);
          if (strategy === 'text') return page.getByText(selector);
          return page.locator(selector);
        }}

        function createCy(page) {{
          let chain = Promise.resolve();
          const api = {{}};

          function wrapLocator(locator) {{
            const chainApi = {{
              click: () => {{ chain = chain.then(() => locator.click()); return chainApi; }},
              dblclick: () => {{ chain = chain.then(() => locator.dblclick()); return chainApi; }},
              rightclick: () => {{ chain = chain.then(() => locator.click({{ button: 'right' }})); return chainApi; }},
              type: (text) => {{ chain = chain.then(() => locator.fill(text)); return chainApi; }},
              clear: () => {{ chain = chain.then(() => locator.fill('')); return chainApi; }},
              fill: (text) => {{ chain = chain.then(() => locator.fill(text)); return chainApi; }},
              select: (value) => {{ chain = chain.then(() => locator.selectOption(value)); return chainApi; }},
              check: () => {{ chain = chain.then(() => locator.check()); return chainApi; }},
              uncheck: () => {{ chain = chain.then(() => locator.uncheck()); return chainApi; }},
              focus: () => {{ chain = chain.then(() => locator.focus()); return chainApi; }},
              blur: () => {{ chain = chain.then(() => locator.blur()); return chainApi; }},
              wait: (ms) => {{ chain = chain.then(() => new Promise((resolve) => setTimeout(resolve, ms))); return chainApi; }},
              first: () => wrapLocator(locator.first()),
              last: () => wrapLocator(locator.last()),
              eq: (index) => wrapLocator(locator.nth(index)),
              find: (sel) => wrapLocator(locator.locator(sel)),
              within: (fn) => {{ chain = chain.then(async () => {{ await fn(); }}); return chainApi; }},
              then: (fn) => {{ chain = chain.then(async () => fn(locator)); return chainApi; }},
              should: (condition, expected) => {{
                chain = chain.then(async () => {{
                  if (condition === 'be.visible') return await locator.waitFor({{ state: 'visible' }});
                  if (condition === 'exist') return assert.ok(await locator.count() > 0, 'Expected element to exist');
                  if (condition === 'not.exist') return assert.ok(await locator.count() === 0, 'Expected element not to exist');
                  if (condition === 'contain.text') return simpleExpect(await locator.textContent()).toContainText(expected);
                  if (condition === 'have.text') return simpleExpect(await locator.textContent()).toHaveText(expected);
                  if (condition === 'have.value') return simpleExpect(await locator.inputValue()).toHaveValue(expected);
                  if (condition === 'have.length') return assert.strictEqual(await locator.count(), expected);
                  if (condition === 'have.attr') return assert.strictEqual(await locator.getAttribute(expected[0] || ''), expected[1]);
                  return assert.ok(true);
                }});
                return chainApi;
              }},
              and: (condition, expected) => {{
                return chainApi.should(condition, expected);
              }},
              invoke: (method, ...args) => {{
                chain = chain.then(async () => {{
                  if (method === 'text') return await locator.textContent();
                  if (method === 'val') return await locator.inputValue();
                  if (method === 'attr') return await locator.getAttribute(args[0]);
                  return undefined;
                }});
                return chainApi;
              }},
              its: (property) => {{
                chain = chain.then(async () => {{
                  if (property === 'length') return await locator.count();
                  return undefined;
                }});
                return chainApi;
              }},
              as: () => chainApi,
            }};
            return chainApi;
          }}

          api.visit = (url) => {{ chain = chain.then(() => page.goto(url)); return api; }};
          api.url = (url) => {{ chain = chain.then(() => page.goto(url)); return api; }};
          api.get = (selector) => wrapLocator(page.locator(selector));
          api.xpath = (selector) => wrapLocator(page.locator(`xpath=${{selector}}`));
          api.contains = (text) => wrapLocator(page.getByText(text));
          api.wait = (ms) => {{ chain = chain.then(() => new Promise((resolve) => setTimeout(resolve, ms))); return api; }};
          api.then = (fn) => {{ chain = chain.then(async () => fn(page)); return api; }};
          api.wrap = wrapLocator;
          api.run = async () => {{ await chain; }};
          return api;
        }}

        function createByHelpers() {{
          return {{
            css: (value) => ({{ strategy: 'css', value }}),
            cssSelector: (value) => ({{ strategy: 'css', value }}),
            xpath: (value) => ({{ strategy: 'xpath', value }}),
            id: (value) => ({{ strategy: 'css', value: `#${{value}}` }}),
            name: (value) => ({{ strategy: 'css', value: `[name="${{value}}"]` }}),
            className: (value) => ({{ strategy: 'css', value: `.${{value}}` }}),
            tagName: (value) => ({{ strategy: 'css', value }}),
            linkText: (value) => ({{ strategy: 'text', value }}),
            partialLinkText: (value) => ({{ strategy: 'text', value }}),
          }};
        }}

        function createSeleniumShim(page) {{
          const By = createByHelpers();

          function locatorFromBy(by) {{
            if (!by) return page.locator('body');
            return locatorFrom(page, by.value, by.strategy);
          }}

          class WebElement {{
            constructor(locator) {{
              this.locator = locator;
            }}
            async click() {{ await this.locator.click(); }}
            async clear() {{ await this.locator.fill(''); }}
            async sendKeys(...values) {{ await this.locator.fill(values.join('')); }}
            async getText() {{ return (await this.locator.textContent()) || ''; }}
            async getAttribute(name) {{ return await this.locator.getAttribute(name); }}
            async isDisplayed() {{ return await this.locator.isVisible(); }}
            async isEnabled() {{ return await this.locator.isEnabled(); }}
            async submit() {{ await this.locator.press('Enter'); }}
            async findElement(by) {{ return new WebElement(locatorFromBy(by)); }}
            async findElements(by) {{ return (await locatorFromBy(by).count()) ? [new WebElement(locatorFromBy(by))] : []; }}
            async getTagName() {{ return await this.locator.evaluate((node) => node.tagName.toLowerCase()); }}
          }}

          class WebDriverWait {{
            constructor(driver, timeoutMs) {{
              this.driver = driver;
              this.timeoutMs = timeoutMs || 5000;
            }}
            async until(condition) {{
              if (typeof condition === 'function') {{
                return await condition(this.driver);
              }}
              return await condition;
            }}
          }}

          const ExpectedConditions = {{
            elementLocated: (by) => async (driver) => new WebElement(locatorFromBy(by)),
            visibilityOfElementLocated: (by) => async (driver) => {{
              const locator = locatorFromBy(by);
              await locator.waitFor({{ state: 'visible', timeout: 5000 }});
              return new WebElement(locator);
            }},
            elementToBeClickable: (by) => async (driver) => {{
              const locator = locatorFromBy(by);
              await locator.waitFor({{ state: 'visible', timeout: 5000 }});
              return new WebElement(locator);
            }},
            titleContains: (text) => async (driver) => {{
              const title = await driver.getTitle();
              assert.ok(String(title).includes(String(text)), `Expected title to contain ${{text}} but got ${{title}}`);
              return true;
            }},
            urlContains: (text) => async (driver) => {{
              const url = await driver.getCurrentUrl();
              assert.ok(String(url).includes(String(text)), `Expected URL to contain ${{text}} but got ${{url}}`);
              return true;
            }},
          }};

          const Key = {{
            ENTER: '\\n',
            TAB: '\\t',
          }};

          const driver = {{
            async get(url) {{ await page.goto(url); }},
            findElement(by) {{ return new WebElement(locatorFromBy(by)); }},
            findElements(by) {{
              const locator = locatorFromBy(by);
              return {{
                async count() {{ return await locator.count(); }},
                async get(index) {{ return new WebElement(locator.nth(index)); }},
                async toArray() {{
                  const count = await locator.count();
                  return Array.from({{ length: count }}, (_, index) => new WebElement(locator.nth(index)));
                }},
              }};
            }},
            async wait(condition, timeoutMs) {{
              if (typeof condition === 'function') {{
                const result = await condition(this);
                return result;
              }}
              if (condition && typeof condition === 'object' && typeof condition.until === 'function') {{
                return await condition.until(this);
              }}
              return await condition;
            }},
            async getTitle() {{ return await page.title(); }},
            async getCurrentUrl() {{ return page.url(); }},
            async quit() {{ }},
            async sleep(ms) {{ await new Promise((resolve) => setTimeout(resolve, ms)); }},
            manage() {{
              return {{
                window() {{
                  return {{
                    maximize: async () => {{}},
                    minimize: async () => {{}},
                    setRect: async () => {{}},
                  }};
                }},
              }};
            }},
            navigate() {{
              return {{
                to: async (url) => await page.goto(url),
                back: async () => await page.goBack(),
                forward: async () => await page.goForward(),
                refresh: async () => await page.reload(),
              }};
            }},
            switchTo() {{
              return {{
                alert: async () => {{
                  const dialog = await page.waitForEvent('dialog');
                  return {{
                    accept: async () => await dialog.accept(),
                    dismiss: async () => await dialog.dismiss(),
                    getText: async () => dialog.message(),
                  }};
                }},
              }};
            }},
            actions() {{
              return {{
                sendKeys: async (...values) => await page.keyboard.type(values.join('')),
              }};
            }},
            _page: page,
          }};

          class Builder {{
            forBrowser() {{ return this; }}
            usingServer() {{ return this; }}
            withCapabilities() {{ return this; }}
            async build() {{ return driver; }}
          }}

          async function createDriver() {{
            return driver;
          }}

          return {{ driver, Builder, By, until: ExpectedConditions, ExpectedConditions, Key, WebDriverWait, WebElement, createDriver }};
        }}

        async function runScript() {{
          const {{ browser, context, page }} = await createBrowser();
          enableVisualFeedback(page);
          const cy = createCy(page);
          const selenium = createSeleniumShim(page);
          const testHooks = {{
            beforeEach: [],
            afterEach: [],
            tests: [],
          }};

          function registerTest(name, fn) {{
            testHooks.tests.push({{ name, fn }});
          }}

          function runBlock(name, fn) {{
            if (typeof fn !== 'function') return;
            try {{
              const result = fn({{ page, browser, context, cy, ...selenium }});
              return result;
            }} catch (error) {{
              throw error;
            }}
          }}

          globalThis.expect = simpleExpect;
          globalThis.describe = (name, fn) => {{ if (typeof fn === 'function') return fn(); }};
          globalThis.it = (name, fn) => registerTest(name, fn);
          globalThis.test = (name, fn) => registerTest(name, fn);
          globalThis.beforeEach = (fn) => testHooks.beforeEach.push(fn);
          globalThis.afterEach = (fn) => testHooks.afterEach.push(fn);
          globalThis.cy = cy;
          globalThis.Builder = selenium.Builder;
          globalThis.By = selenium.By;
          globalThis.until = selenium.until;
          globalThis.ExpectedConditions = selenium.ExpectedConditions;
          globalThis.WebDriverWait = selenium.WebDriverWait;
          globalThis.Key = selenium.Key;
          globalThis.createDriver = selenium.createDriver;
          globalThis.driver = selenium.driver;
          globalThis.System = {{
            out: {{
              println: (...args) => console.log(...args),
              print: (...args) => process.stdout.write(args.join(' ')),
            }},
            err: {{
              println: (...args) => console.error(...args),
              print: (...args) => process.stderr.write(args.join(' ')),
            }},
            getProperty: (name, defaultValue = '') => process.env[name] || defaultValue,
            setProperty: () => undefined,
            currentTimeMillis: () => Date.now(),
            exit: (code = 0) => {{
              throw new Error(`System.exit(${{code}})`);
            }},
          }};
          globalThis.sleep = async (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          globalThis.assertTrue = (value, message) => assert.ok(value, message);
          globalThis.assertFalse = (value, message) => assert.ok(!value, message);
          globalThis.assertEquals = (expected, actual, message) => assert.strictEqual(actual, expected, message);
          globalThis.assertNotNull = (value, message) => assert.ok(value !== null && value !== undefined, message);
          globalThis.assertNull = (value, message) => assert.ok(value === null || value === undefined, message);

          {body}

          for (const testCase of testHooks.tests) {{
            for (const hook of testHooks.beforeEach) {{
              await runBlock('beforeEach', hook);
            }}
            await runBlock(testCase.name, testCase.fn);
            for (const hook of testHooks.afterEach) {{
              await runBlock('afterEach', hook);
            }}
          }}

          await cy.run();
          await browser.close();
        }}

        runScript().catch((error) => {{
          console.error(error && error.stack ? error.stack : error);
          process.exit(1);
        }});
        """
    ).strip()
