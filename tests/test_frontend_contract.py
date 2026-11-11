import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
REVIEWS_HTML = (Path(__file__).resolve().parents[1] / "reviews.html").read_text(encoding="utf-8")
TIMETABLE_EXPORT_JS = (Path(__file__).resolve().parents[1] / "Images/timetable-export.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def function_body(name, page=HTML):
    """Return a complete JS function body without stopping at nested braces."""
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", page)
    if not match:
        raise AssertionError(f"JavaScript function {name!r} is missing")

    start = match.end()
    depth = 1
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    index = start

    while index < len(page):
        char = page[index]
        nxt = page[index + 1] if index + 1 < len(page) else ""

        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char == "/" and nxt == "/":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return page[start:index]
        index += 1

    raise AssertionError(f"JavaScript function {name!r} has an unclosed body")


def function_source(name, page=HTML):
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", page)
    if not match:
        raise AssertionError(f"JavaScript function {name!r} is missing")
    return match.group(0) + function_body(name, page) + "}"


def material_styles(page):
    begin = "/* ===== Material and motion — shared by both pages ===== */"
    end = "/* ===== End material and motion ===== */"
    if page.count(begin) != 1 or page.count(end) != 1:
        raise AssertionError("The shared visual layer must appear exactly once")
    start, finish = page.index(begin), page.index(end)
    if not page.index("<style>") < start < finish < page.index("</style>"):
        raise AssertionError("The visual layer must remain entirely inside the stylesheet")
    return page[start + len(begin):finish]


def css_rule_blocks(source, parents=()):
    """Read this layer's nested media/keyframe rules without a CSS dependency."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    offset = 0
    while (opening := source.find("{", offset)) != -1:
        selector = source[offset:opening].strip()
        depth, closing = 1, opening + 1
        while depth and closing < len(source):
            depth += (source[closing] == "{") - (source[closing] == "}")
            closing += 1
        if depth:
            raise AssertionError("Unclosed shared CSS rule")
        body = source[opening + 1:closing - 1]
        if selector.startswith("@"):
            yield from css_rule_blocks(body, (*parents, selector))
        else:
            declarations = dict(
                (name.strip(), value.strip())
                for entry in body.split(";") if ":" in entry
                for name, value in [entry.split(":", 1)]
            )
            yield parents, selector, declarations
        offset = closing


class FrontendContractTests(unittest.TestCase):
    def test_shared_visual_layer_preserves_interaction_structure(self):
        block = material_styles(HTML)
        self.assertEqual(block, material_styles(REVIEWS_HTML))
        # State, focusability and the existing stacked-dialog geometry belong to
        # the original UI rules and handlers, not to this optional visual layer.
        forbidden = {"position", "z-index", "pointer-events", "visibility", "overflow", "overflow-x", "overflow-y"}
        for _, selector, declarations in css_rule_blocks(block):
            self.assertFalse(forbidden & declarations.keys(), selector)
            self.assertNotRegex(selector, r"\[(?:hidden|inert)\]")
            if "display" in declarations:
                self.assertEqual(selector, ".course-card::after")
                self.assertEqual(declarations["display"], "none")

    def test_material_effects_stay_within_scroll_and_motion_budgets(self):
        rules = list(css_rule_blocks(material_styles(HTML)))
        sampled_surfaces = {".topbar", ".custom-select-dropdown", ".popular-courses"}
        touch_rules = []
        cards_static = ambient_static = False
        for parents, selector, declarations in rules:
            classes = set(re.findall(r"\.[\w-]+", selector))
            touch = any("pointer: coarse" in parent or "max-width:" in parent for parent in parents)
            if touch:
                touch_rules.append((classes, declarations))
            for prop, value in declarations.items():
                if prop.endswith("backdrop-filter") and value != "none":
                    self.assertTrue(classes and classes <= sampled_surfaces, selector)
                    radius = re.search(r"blur\(([\d.]+)px\)", value)
                    self.assertIsNotNone(radius, selector)
                    self.assertLessEqual(float(radius.group(1)), 10 if touch else 16)
                if prop in {"transition", "transition-property"}:
                    self.assertNotRegex(value, r"\ball\b", selector)
                if prop == "filter":
                    self.assertEqual(value, "none", selector)
                if prop == "will-change":
                    self.assertEqual(value, "auto", selector)
                if prop.startswith("animation"):
                    self.assertNotIn("infinite", value, selector)
            if any(parent.startswith("@keyframes") for parent in parents):
                self.assertTrue(declarations.keys() <= {"opacity", "transform"}, selector)
            if not parents and {".course-card", ".thread"} <= classes:
                cards_static |= declarations.get("animation") == "none"
            if not parents and selector == "body::before":
                ambient_static |= declarations.get("animation") == declarations.get("filter") == "none"
        self.assertTrue(cards_static, "Growing result lists must not animate every appended card")
        self.assertTrue(ambient_static, "The viewport-wide ambient layer must not keep animating or blurring")
        self.assertTrue(any(
            {".custom-select-dropdown", ".popular-courses"} <= classes
            and declarations.get("backdrop-filter") == declarations.get("-webkit-backdrop-filter") == "none"
            and declarations.get("background-color") in {"var(--surface)", "var(--glass-solid)"}
            for classes, declarations in touch_rules
        ), "Touch menus must use opaque surfaces without sampled blur")

    def test_material_layer_respects_system_preferences_and_blur_fallback(self):
        rules = list(css_rule_blocks(material_styles(HTML)))
        reduced_motion = [
            (selector, declarations) for parents, selector, declarations in rules
            if any("prefers-reduced-motion: reduce" in parent for parent in parents)
        ]
        self.assertTrue(any(
            {"*", "*::before", "*::after"} <= {part.strip() for part in selector.split(",")}
            and declarations.get("animation") == declarations.get("transition") == "none !important"
            for selector, declarations in reduced_motion
        ), "Reduced motion must stop repeated skeleton/spinner animations, not only shorten them")
        self.assertTrue(any(selector == "html" and declarations.get("scroll-behavior") == "auto"
                            for selector, declarations in reduced_motion))
        required_surfaces = {".topbar", ".custom-select-dropdown", ".popular-courses"}
        for preference in ("prefers-reduced-transparency: reduce", "prefers-contrast: more"):
            self.assertTrue(any(
                any(preference in parent for parent in parents)
                and required_surfaces <= set(re.findall(r"\.[\w-]+", selector))
                and declarations.get("background") == "var(--surface)"
                and declarations.get("backdrop-filter") == declarations.get("-webkit-backdrop-filter") == "none"
                for parents, selector, declarations in rules
            ), preference)
        self.assertTrue(any(
            any(parent.startswith("@supports not") and "backdrop-filter" in parent for parent in parents)
            and required_surfaces <= set(re.findall(r"\.[\w-]+", selector))
            and declarations.get("background") in {"var(--surface)", "var(--glass-solid)"}
            for parents, selector, declarations in rules
        ), "Browsers without blur support must receive an opaque fallback")

    def test_translation_is_disabled_for_all_terms_and_course_fields(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            const TR_IDX = {{ en: 0, ja: 1 }};
            let currentLang = 'en';
            {function_source('displayLanguageForTerm')}
            {function_source('trCourseName')}
            assert.equal(displayLanguageForTerm('en', 'fall'), 'zh');
            assert.equal(displayLanguageForTerm('ja', 'fall'), 'zh');
            assert.equal(displayLanguageForTerm('en', 'spring'), 'zh');
            assert.equal(displayLanguageForTerm('bad', 'summer'), 'zh');
            assert.equal(trCourseName({{ id: 'a1', course_name: '中文课名', english_name: 'English' }}), '中文课名');
            assert.equal(trCourseName({{ id: 'r1', course_name: '研究生课', english_name: 'English' }}), '研究生课');
            assert.equal(trCourseName({{ id: 'a1', course_name: '中文课名', english_name: '  ' }}), '中文课名');
            assert.equal(trCourseName({{ id: 'r1', course_name: 'Translated course' }}), 'Translated course');
            assert.equal(trCourseName({{ id: 'u1', course_name: '春季课', english_name: 'English' }}), '春季课');
        """)
        self.assertIn('displayLanguageForTerm(currentLang, currentTerm)', function_body('readURLState'))
        self.assertIn('displayLanguageForTerm(key, currentTerm)', function_body('setLang'))
        self.assertIn('refreshLangSelectorUI()', function_body('setTerm'))
        self.assertIn('id="langSelector" hidden', HTML)
        self.assertNotIn("p.get('lang')", function_body('readURLState'))
        self.assertNotIn("localStorage.getItem('pinhaoke_lang')", HTML)
        self.assertIn('.font-option[hidden] { display: none; }', HTML)

    def test_timetable_week_matching_and_safe_personal_rendering(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            {function_source('sessionInWeek')}
            {function_source('sessionsOverlap')}
            const odd = {{weeks:[1,3,5], parity:'单周'}};
            const even = {{weeks:[2,4,6], parity:'双周'}};
            assert.equal(sessionsOverlap(odd, even), false);
            assert.equal(sessionInWeek(odd, 3), true);
            assert.equal(sessionInWeek(odd, 2), false);
            assert.equal(sessionInWeek({{weeks:[0,1], parity:'每周'}}, 0), true);
            assert.equal(sessionInWeek(odd, -1), true);
        """)
        self.assertNotIn('innerHTML', function_body('renderTimetable'))
        self.assertIn("createTimetableButton(c)", function_body('showDetail'))
        self.assertIn("favToggle.before(timetableAction)", function_body('showDetail'))
        self.assertIn("createCard(item", function_body('buildFavItem'))

    def test_timetable_button_toggles_and_preserves_state_on_failure(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            let favUser = {{username:'qa'}}, timetableCourses = [], timetableLoaded = false;
            let timetableLoading = false, timetableRequest = 0, accountViewOpen = false, favSessionVersion = 0;
            let failRemove = false, callback, loaded = 0, added = 0;
            const ICONS = {{calendar:'<svg></svg>'}};
            const course = {{id:'a1'}}, favoriteKey = () => 'stable-key';
            const favButton = (text, cls, cb) => {{ callback=cb; return {{dataset:{{}}, appendChild(){{}}, setAttribute(){{}}}}; }};
            const favEl = () => ({{}}), favToast = () => {{}}, updateTimetableButtons = () => {{}};
            const favErrorText = () => 'error';
            const loadTimetable = async () => {{ loaded++; timetableLoaded=true; }};
            const addToTimetable = async () => {{ added++; timetableCourses=[{{id:'a1',course_key:'stable-key'}}]; }};
            const favApi = async (method,url,payload) => {{
                assert.equal(url,'/api/timetable/remove');
                assert.equal(payload.course_key,'stable-key');
                return failRemove ? {{ok:false}} : {{ok:true,data:{{courses:[]}}}};
            }};
            {function_source('timetableHas')}
            {function_source('removeFromTimetable')}
            {function_source('createTimetableButton')}
            (async () => {{
                const button=createTimetableButton(course);
                await callback(); assert.equal(added,1); assert.equal(loaded,1);
                failRemove=true; await callback(); assert.equal(timetableCourses.length,1); assert.equal(button.disabled,false);
                failRemove=false; await callback(); assert.equal(timetableCourses.length,0);
                await callback(); assert.equal(added,2); assert.equal(button.disabled,false);
            }})().catch(error => {{ console.error(error); process.exit(1); }});
        """)

    def test_timetable_export_preserves_wrapped_text_and_bounds_canvas_memory(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            {function_source('wrapCanvasText', TIMETABLE_EXPORT_JS)}
            {function_source('timetableImageScale', TIMETABLE_EXPORT_JS)}
            const context = {{measureText: text => ({{width: [...text].length * 10}})}};
            const title = '一门非常长的课程名 Mathematics 🧪 与实验';
            const lines = wrapCanvasText(context, title, 70);
            assert.equal(lines.join(''), title);
            assert.ok(lines.every(line => context.measureText(line).width <= 70));
            assert.deepEqual(wrapCanvasText(context, '理教101\\n二教201', 200), ['理教101','二教201']);
            for (const [width, height] of [[1460,1000],[1460,4000],[1460,20000]]) {{
                const scale = timetableImageScale(width, height);
                assert.ok(scale > 0 && scale <= 2);
                assert.ok(width * scale <= 4096 && height * scale <= 4096);
                assert.ok(width * height * scale * scale <= 8000001);
            }}
        """)

    def test_personal_timetable_week_input_preserves_zero_and_rejects_invalid_ranges(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            {function_source('parseTimetableWeeks')}
            assert.equal(parseTimetableWeeks('  '), null);
            assert.deepEqual(parseTimetableWeeks('0-3, 5，3、8~9'), [0,1,2,3,5,8,9]);
            for (const text of ['16-1','-1','31','1.5','1,','a','1-2-3']) assert.throws(()=>parseTimetableWeeks(text));
        """)
        for name in ['openTimetableEditor', 'timetableTimeRow', 'renderTimetable']:
            self.assertNotIn('innerHTML', function_body(name))
        self.assertIn('textContent', function_body('openTimetableEditor'))
        self.assertIn('timetableEditor', function_body('closeAccountView'))

    def test_personal_timetable_save_keeps_state_on_failure_and_cancels_old_account_response(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            let favUser={{username:'A'}}, favSessionVersion=1, timetableRequest=0, timetableLoading=false;
            let timetableCourses=[{{course_key:'old'}}],timetableLoaded=true,timetableTerm='fall',accountViewOpen=false;
            const updateTimetableButtons=()=>{{}};
            let finish, calls=0;
            const favApi=()=>{{calls++;return new Promise(resolve=>finish=resolve);}};
            {function_source('saveTimetableChanges')}
            (async()=>{{
                const editor={{key:'custom:abc',sessionVersion:1}};
                let saving=saveTimetableChanges('/api/timetable/update',{{}},editor);
                finish({{ok:false,status:503}});await saving;
                assert.deepEqual(timetableCourses,[{{course_key:'old'}}]);
                saving=saveTimetableChanges('/api/timetable/custom',{{}},editor);
                finish({{ok:true,data:{{courses:[{{course_key:'custom:abc',term:'summer'}}]}}}});await saving;
                assert.equal(timetableTerm,'summer');
                saving=saveTimetableChanges('/api/timetable/reset',{{}},editor);
                favSessionVersion=2;favUser={{username:'B'}};timetableCourses=[];
                finish({{ok:true,data:{{courses:[{{course_key:'private-A'}}]}}}});
                assert.equal((await saving).stale,true);assert.deepEqual(timetableCourses,[]);
                assert.equal((await saveTimetableChanges('/api/timetable/update',{{}},editor)).stale,true);
                assert.equal(calls,3);
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_course_messages_are_first_and_rendered_safely(self):
        self.assertIn("mountCourseMessages(content.querySelector('.modal-body'), c)", function_body('showDetail'))
        body = function_body('mountCourseMessages')
        self.assertIn('host.prepend(summary)', body)
        self.assertIn('summary.isConnected', body)
        self.assertIn('currentModalCourseId === course.id', body)
        self.assertIn('sequence !== request', body)
        self.assertIn('encodeURIComponent(course.id)', body)
        self.assertIn('renderMessage(message)', body)
        self.assertIn('JSON.stringify({content})', body)
        self.assertNotIn('innerHTML', body)
        self.assertIn('openCourseCorrection(section, course)', body)
        self.assertIn('-webkit-line-clamp: 2', HTML)
        self.assertIn('这里用于补充或修正课程信息', HTML)
        self.assertIn("history.pushState({ phk: 'coursecorrection' }", function_body('openCourseCorrection'))
        summary_style = re.search(r'\.course-message-summary \{([^}]+)\}', HTML).group(1)
        self.assertIn('border: 1px solid var(--border-soft)', summary_style)
        self.assertNotIn('2px solid', summary_style)
        self.assertNotIn('border-left', summary_style)
        has_notes_style = re.search(r'\.course-message-summary\.has-notes \{([^}]+)\}', HTML).group(1)
        self.assertIn('border: 2px solid #18B89A', has_notes_style)
        self.assertIn("summary.classList.toggle('has-notes', course.has_course_corrections === true)", body)
        self.assertIn("summary.classList.toggle('has-notes', total > 0)", body)
        self.assertIn("summary.classList.add('has-notes')", body)

    def test_course_correction_status_updates_matching_cards_without_duplicates(self):
        self.assertIn('setCourseCorrectionIndicator(card, course.has_course_corrections === true)', function_body('createCard'))
        self.assertIn('syncCourseCorrectionIndicators(course.id, true)', function_body('mountCourseMessages'))
        self.assertIn('syncCourseCorrectionIndicators(course.id, total > 0)', function_body('mountCourseMessages'))
        self.run_node(f"""
            const assert = require('node:assert/strict');
            function element() {{
              return {{ children: [], dataset: {{}}, classes: new Set(),
                classList: {{ toggle(name, value) {{ value ? this.owner.classes.add(name) : this.owner.classes.delete(name); }} }},
                setAttribute(name, value) {{ this[name] = value; }},
                append(...children) {{ children.forEach(child => {{ child.parent = this; this.children.push(child); }}); }},
                querySelector() {{ return this.children.find(child => child.className === 'card-correction'); }},
                remove() {{ this.parent.children = this.parent.children.filter(child => child !== this); }} }};
            }}
            const cards = [element(), element()];
            cards.forEach((card, index) => {{ card.dataset.courseId = 'a' + (index + 1); card.classList.owner = card; }});
            const currentCourses = [{{id:'a1'}}, {{id:'a2'}}];
            const document = {{ createElement: element, createTextNode: text => ({{textContent:text}}), querySelectorAll: () => cards }};
            {function_source('setCourseCorrectionIndicator')}
            {function_source('syncCourseCorrectionIndicators')}
            syncCourseCorrectionIndicators('a1', true);
            syncCourseCorrectionIndicators('a1', true);
            assert.equal(cards[0].children.length, 1);
            assert.equal(cards[0].children[0].children[1].textContent, '内容有修正');
            assert.equal(cards[0].children[0].children[0]['aria-hidden'], 'true');
            assert.equal(cards[1].children.length, 0);
            assert.equal(currentCourses[0].has_course_corrections, true);
            syncCourseCorrectionIndicators('a1', false);
            assert.equal(cards[0].children.length, 0);
            assert.equal(cards[0].classes.has('has-corrections'), false);
            assert.equal(currentCourses[0].has_course_corrections, false);
        """)

    def test_collection_debounce_keeps_course_snapshot_and_discards_old_session(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            let currentKey = 'course-A', currentIds = [1], favSessionVersion = 0;
            let favUser = {{username: 'qa'}}, timerId = 0;
            const timers = new Map(), favCollTimers = new Map();
            const favItems = new Map([['course-A', {{}}]]), posts = [];
            const setTimeout = callback => {{ timers.set(++timerId, callback); return timerId; }};
            const clearTimeout = id => timers.delete(id);
            const document = {{getElementById: id => id === 'favToggleBtn'
              ? {{dataset: {{key: currentKey}}}}
              : {{querySelectorAll: () => currentIds.map(value => ({{value}}))}}}};
            const favApi = async (method, path, body) => {{ posts.push(body); return {{ok:true,data:{{}}}}; }};
            const applyFavData = () => {{}}, refreshDetailChooser = () => {{}}, favToast = () => {{}};
            {function_source('scheduleSetCollections')}
            (async () => {{
              scheduleSetCollections('course-A');
              currentIds = [1, 3]; scheduleSetCollections('course-A');
              assert.equal(timers.size, 1);
              currentKey = 'course-B'; currentIds = [2];
              await [...timers.values()][0]();
              assert.deepEqual(posts, [{{fav_key:'course-A',collection_ids:[1,3]}}]);
              timers.clear(); currentKey = 'course-A';
              scheduleSetCollections('course-A'); favSessionVersion++;
              await [...timers.values()][0]();
              assert.equal(posts.length, 1);
              timers.clear(); scheduleSetCollections('course-A'); favItems.clear();
              await [...timers.values()][0]();
              assert.equal(posts.length, 1);
            }})().catch(error => {{console.error(error); process.exit(1);}});
        """)

    def test_review_pagination_retries_failed_page_without_discarding_results(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            const PAGE_SIZE=20, requested=[];
            const state={{query:'',page:1,loaded:20,total:60,loading:false,failed:false,requestId:0,fetchController:null}};
            const resultsEl={{children:['page-one'],
              replaceChildren(...children){{this.children=children;}},
              appendChild(child){{this.children.push(child);}},
              querySelector(){{ const found=this.children.find(child => child.id==='reviewLoadError');
                return found ? {{remove:()=>{{this.children=this.children.filter(child=>child!==found);}}}} : null; }} }};
            const document={{createDocumentFragment:()=>({{children:[],appendChild(child){{this.children.push(child);}}}})}};
            const loadMoreButton={{querySelector:()=>({{}})}};
            const makeElement=()=>({{setAttribute(){{}}}}), renderThread=thread=>thread;
            const renderState=()=>resultsEl.replaceChildren('error'), renderSkeletons=()=>resultsEl.replaceChildren('skeleton');
            const updateResultHeader=()=>{{}};
            let fail=true;
            const fetch=async url=>{{
              requested.push(Number(new URL(url,'http://local').searchParams.get('page')));
              return {{ok:!fail,status:503,json:async()=>({{threads:[{{pid:21}}],total:60}})}};
            }};
            {function_source('updateLoadMore', REVIEWS_HTML)}
            {function_source('fetchReviews', REVIEWS_HTML)}
            (async()=>{{
              await fetchReviews();
              assert.equal(state.page,1); assert.equal(state.loaded,20);
              assert.equal(resultsEl.children[0],'page-one'); assert.equal(state.failed,true);
              assert.equal(loadMoreButton.hidden,false); assert.equal(loadMoreButton.disabled,false);
              fail=false; await fetchReviews();
              assert.deepEqual(requested,[2,2]); assert.equal(state.page,2); assert.equal(state.loaded,21);
              assert.equal(resultsEl.children[0],'page-one'); assert.equal(resultsEl.children.length,2);
              fail=true; await fetchReviews({{reset:true}});
              assert.equal(state.loaded,0); assert.equal(state.failed,true); assert.equal(loadMoreButton.hidden,false);
              fail=false; await fetchReviews({{reset:true}});
              assert.deepEqual(requested,[2,2,1,1]); assert.equal(state.page,1); assert.equal(state.loaded,1);
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_account_refresh_cannot_restore_logged_out_or_superseded_state(self):
        self.run_node(f"""
            const assert=require('node:assert/strict');
            let favUser={{username:'old'}}, favSessionVersion=0, accountRefreshRequest=0;
            const pending=[], favApi=()=>new Promise(resolve=>pending.push(resolve));
            const applyAccount=data=>{{favUser={{username:data.username}};}}, setFavUser=user=>{{favUser=user;favSessionVersion++;}};
            {function_source('refreshAccount')}
            (async()=>{{
              const beforeLogout=refreshAccount(); setFavUser(null);
              pending.shift()({{ok:true,data:{{authenticated:true,username:'old'}}}});
              assert.equal(await beforeLogout,false); assert.equal(favUser,null);
              const first=refreshAccount(), second=refreshAccount();
              const resolveFirst=pending.shift(),resolveSecond=pending.shift();
              resolveSecond({{ok:true,data:{{authenticated:true,username:'new'}}}}); assert.equal(await second,true);
              resolveFirst({{ok:true,data:{{authenticated:true,username:'old'}}}}); assert.equal(await first,false);
              assert.equal(favUser.username,'new');
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_saved_course_api_serializes_writes_and_ignores_old_session_responses(self):
        self.run_node(f"""
            const assert=require('node:assert/strict');
            let favSessionVersion=0,accountRefreshRequest=0,favMutationQueue=Promise.resolve(),favUser={{username:'old'}};
            let favAuthBusy=false,favCookieQueue=Promise.resolve();
            const favCollTimers=new Map(),requests=[];
            const favToast=()=>{{}},setFavUser=user=>{{favUser=user;invalidateFavSession();}};
            const fetch=(url,options)=>new Promise(resolve=>requests.push({{url,options,resolve}}));
            const finish=(request,status,data)=>request.resolve({{status,ok:status>=200&&status<300,json:async()=>data}});
            const tick=()=>new Promise(resolve=>setImmediate(resolve));
            {function_source('invalidateFavSession')}
            {function_source('favApi')}
            (async()=>{{
              const a=favApi('POST','/api/favorites',{{id:'a1'}});
              const b=favApi('POST','/api/favorites',{{id:'a2'}});
              await tick(); assert.equal(requests.length,1);
              finish(requests[0],200,{{favorites:[1]}}); assert.equal((await a).ok,true);
              await tick(); assert.equal(requests.length,2);
              finish(requests[1],200,{{favorites:[1,2]}}); assert.equal((await b).ok,true);
              assert.equal(accountRefreshRequest,2);
              const oldRead=favApi('GET','/api/account');
              const oldWrite=favApi('POST','/api/favorites',{{id:'a3'}});
              await tick();
              const queuedOldWrite=favApi('POST','/api/favorites',{{id:'a4'}});
              setFavUser(null); favUser={{username:'new'}};
              finish(requests[2],401,{{}}); assert.equal((await oldRead).stale,true);
              assert.equal(favUser.username,'new');
              finish(requests[3],200,{{favorites:[1,2,3]}}); assert.equal((await oldWrite).stale,true);
              assert.equal((await queuedOldWrite).stale,true); assert.equal(requests.length,4);
              const login=favApi('POST','/api/auth/login',{{username:'next'}});
              await tick();
              finish(requests[4],200,{{}}); const version=favSessionVersion;
              assert.equal((await login).ok,true); assert.equal(favSessionVersion,version+1);
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_auth_requests_remain_exclusive_across_form_changes_and_cookie_reads(self):
        self.run_node(f"""
            const assert=require('node:assert/strict');
            let favUser=null,favSessionVersion=0,accountRefreshRequest=0,favMutationQueue=Promise.resolve();
            let favAuthBusy=false,favCookieQueue=Promise.resolve(),favBusy=false,browserCookie='old';
            const favCollTimers=new Map(),requests=[];
            const favToast=()=>{{}},setFavUser=user=>{{favUser=user;invalidateFavSession();}};
            const fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({{url,options,resolve,reject}}));
            const finish=(index,cookie,data={{}})=>{{
              browserCookie=cookie;
              requests[index].resolve({{status:200,ok:true,json:async()=>data}});
            }};
            const tick=()=>new Promise(resolve=>setImmediate(resolve));
            {function_source('invalidateFavSession')}
            {function_source('favApi')}
            {function_source('favErrorText')}
            (async()=>{{
              const initialRead=favApi('GET','/api/account');await tick();
              const login=favApi('POST','/api/auth/login',{{username:'A'}});
              // 标签切换会重置表单 busy，但不能解除独立的认证互斥。
              favBusy=false;
              for(const action of ['login','register','logout','reset','delete']){{
                const blocked=await favApi('POST','/api/auth/'+action,{{username:'B'}});
                assert.equal(blocked.authBusy,true);
                assert.equal(favErrorText(blocked,{{409:'duplicate name'}}),'账号操作正在进行，请稍后重试');
              }}
              for(const endpoint of ['favorites','collections','timetable']){{
                assert.equal((await favApi('POST','/api/'+endpoint,{{id:'a1'}})).authBusy,true);
              }}
              assert.equal(requests.length,1);
              finish(0,'',{{authenticated:false}});await initialRead;await tick();
              assert.equal(requests.length,2);assert.equal(requests[1].url,'/api/auth/login');
              const accountRead=favApi('GET','/api/account');
              // 较早的账号读取重绘了本地状态，认证结果仍须与浏览器已应用的 Cookie 同步。
              favSessionVersion++;
              finish(1,'A',{{username:'A'}});const loggedIn=await login;
              assert.equal(loggedIn.ok,true);assert.equal(loggedIn.stale,undefined);assert.equal(browserCookie,'A');
              assert.equal(favUser,null);
              assert.equal(favAuthBusy,false);await tick();
              assert.equal(requests.length,3);assert.equal(requests[2].url,'/api/account');
              const logout=favApi('POST','/api/auth/logout');await tick();
              assert.equal(requests.length,3);
              finish(2,'A',{{authenticated:true,username:'A'}});await accountRead;await tick();
              assert.equal(requests.length,4);assert.equal(requests[3].url,'/api/auth/logout');
              finish(3,'');assert.equal((await logout).ok,true);assert.equal(browserCookie,'');
              const failed=favApi('POST','/api/auth/register',{{username:'B'}});await tick();
              requests[4].reject(new Error('network'));assert.equal((await failed).ok,false);assert.equal(favAuthBusy,false);
              const retry=favApi('POST','/api/auth/register',{{username:'B'}});await tick();
              finish(5,'B',{{username:'B'}});assert.equal((await retry).ok,true);assert.equal(browserCookie,'B');
              favUser={{username:'B'}};
              const incorrect=favApi('POST','/api/auth/login',{{username:'C'}});await tick();
              requests[6].resolve({{status:401,ok:false,json:async()=>({{}})}});
              assert.equal((await incorrect).status,401);assert.equal(favUser.username,'B');assert.equal(browserCookie,'B');
              const saving=favApi('POST','/api/favorites',{{id:'a1'}});
              const savingNext=favApi('POST','/api/timetable',{{id:'a2'}});
              const signingOut=favApi('POST','/api/auth/logout');await tick();
              assert.equal(requests.length,8);assert.equal(requests[7].url,'/api/favorites');
              assert.equal((await favApi('POST','/api/timetable',{{id:'a2'}})).authBusy,true);
              finish(7,'B',{{favorites:[]}});await saving;await tick();
              assert.equal(requests.length,9);assert.equal(requests[8].url,'/api/timetable');
              finish(8,'B',{{courses:[]}});await savingNext;await tick();
              assert.equal(requests.length,10);assert.equal(requests[9].url,'/api/auth/logout');
              finish(9,'');assert.equal((await signingOut).ok,true);
              assert.equal(favUser,null);assert.equal(browserCookie,'');
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_expired_favorite_removal_does_not_restore_private_state(self):
        self.run_node(f"""
            const assert=require('node:assert/strict');
            let favUser={{username:'qa'}},favSessionVersion=0;
            const favItems=new Map([['key',{{course_name:'course'}}]]),favCollTimers=new Map();
            const trCourseName=course=>course.course_name,favoriteKey=()=> 'key',refreshFavoriteButtons=()=>{{}};
            let expire=true;const notices=[],favLimit=300,favToast=text=>notices.push(text);
            const favApi=async()=>{{
              if(!expire)return {{ok:false,status:409,authBusy:true}};
              favUser=null;favSessionVersion++;favItems.clear();return {{ok:false,status:401}};
            }};
            {function_source('favErrorText')}
            {function_source('toggleFavorite')}
            (async()=>{{
              await toggleFavorite({{id:'a1',course_name:'course'}});
              assert.equal(favUser,null); assert.equal(favItems.size,0);
              expire=false;favUser={{username:'qa'}};favItems.set('key',{{course_name:'course'}});
              await toggleFavorite({{id:'a1',course_name:'course'}});
              assert.equal(favItems.get('key').course_name,'course');
              favItems.clear();await toggleFavorite({{id:'a1',course_name:'course'}});
              assert.equal(favItems.size,0);
              assert.deepEqual(notices,['账号操作正在进行，请稍后重试','账号操作正在进行，请稍后重试']);
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_timetable_queued_success_survives_later_failure_and_stale_reads(self):
        self.run_node(f"""
            const assert=require('node:assert/strict');
            let favUser={{username:'qa'}},timetableRequest=0,timetableLoading=false,timetableCourses=[];
            let timetableLoaded=false,timetableTerm='fall',accountViewOpen=false,accountSection='timetable';
            let favSessionVersion=0,accountRefreshRequest=0,favMutationQueue=Promise.resolve();
            let favAuthBusy=false,favCookieQueue=Promise.resolve();
            const favCollTimers=new Map(),requests=[];
            const fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({{url,options,resolve,reject}}));
            const finish=(index,courses)=>requests[index].resolve({{status:200,ok:true,json:async()=>({{courses}})}});
            const tick=()=>new Promise(resolve=>setImmediate(resolve));
            const updateTimetableButtons=()=>{{}},renderAccountView=()=>{{}},favToast=()=>{{}},favErrorText=()=> 'network';
            const setFavUser=user=>{{favUser=user;invalidateFavSession();}};
            {function_source('invalidateFavSession')}
            {function_source('favApi')}
            {function_source('loadTimetable')}
            {function_source('addToTimetable')}
            {function_source('removeFromTimetable')}
            (async()=>{{
              const a=addToTimetable('a1'),b=addToTimetable('a2');
              await tick();assert.equal(requests.length,1);
              finish(0,[{{id:'a1'}}]);await a;
              assert.deepEqual(timetableCourses,[{{id:'a1'}}]);
              await tick();assert.equal(requests.length,2);
              requests[1].reject(new Error('network'));await b;
              assert.deepEqual(timetableCourses,[{{id:'a1'}}]);

              const adding=addToTimetable('a3');await tick();
              const reading=loadTimetable();
              assert.equal(requests[2].options.method,'POST');assert.equal(requests[3].options.method,'GET');
              finish(2,[{{id:'a1'}},{{id:'a3'}}]);await adding;
              finish(3,[{{id:'a1'}}]);await reading;
              assert.deepEqual(timetableCourses,[{{id:'a1'}},{{id:'a3'}}]);

              const removing=removeFromTimetable('a1'),later=addToTimetable('a4');
              await tick();assert.equal(requests.length,5);
              finish(4,[{{id:'a3'}}]);assert.equal(await removing,true);
              await tick();requests[5].reject(new Error('network'));await later;
              assert.deepEqual(timetableCourses,[{{id:'a3'}}]);

              const oldRead=loadTimetable(),oldWrite=addToTimetable('a5');await tick();
              invalidateFavSession();timetableCourses=[{{id:'new-session'}}];
              finish(6,[{{id:'a3'}}]);await oldRead;
              finish(7,[{{id:'a3'}},{{id:'a5'}}]);await oldWrite;
              assert.deepEqual(timetableCourses,[{{id:'new-session'}}]);
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def test_timetable_click_cannot_continue_after_account_changes_while_loading(self):
        self.run_node(f"""
            const assert=require('node:assert/strict');
            let favUser={{username:'A'}},favSessionVersion=0,timetableLoaded=false,timetableCourses=[];
            let callback,finishLoad,adds=0,removes=0;
            const ICONS={{calendar:'<svg></svg>'}},favEl=()=>({{}}),favoriteKey=()=> 'key';
            const favButton=(text,cls,handler)=>{{callback=handler;return {{dataset:{{}},appendChild(){{}},setAttribute(){{}}}};}};
            const loadTimetable=()=>new Promise(resolve=>finishLoad=resolve);
            const addToTimetable=async()=>{{adds++;}},removeFromTimetable=async()=>{{removes++;}};
            {function_source('timetableHas')}
            {function_source('createTimetableButton')}
            (async()=>{{
              const button=createTimetableButton({{id:'a1'}}),pending=callback();
              assert.equal(button.disabled,true);
              favUser={{username:'B'}};favSessionVersion++;timetableLoaded=true;
              finishLoad();await pending;
              assert.equal(adds,0);assert.equal(removes,0);assert.equal(button.disabled,false);
            }})().catch(error=>{{console.error(error);process.exit(1);}});
        """)

    def run_node(self, script):
        if not NODE:
            self.skipTest("node is unavailable; JavaScript behavior contract skipped")
        result = subprocess.run(
            [NODE, "-e", textwrap.dedent(script)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_treehole_entry_is_temporarily_hidden_and_terms_stay_ordered(self):
        spring = HTML.index('id="termSpringBtn"')
        summer = HTML.index('id="termSummerBtn"')
        fall = HTML.index('id="termFallBtn"')
        reviews = HTML.index('id="reviewHubLink"')
        self.assertLess(spring, summer)
        self.assertLess(summer, fall)
        self.assertLess(fall, reviews)
        anchor = HTML[reviews:reviews + 180]
        self.assertIn('href="/reviews"', anchor)
        self.assertIn(" hidden>", anchor)
        self.assertIn('<div class="review-nav-shell" hidden>', REVIEWS_HTML)

    def test_message_board_button_and_panel_contract(self):
        # 两页顶栏都有留言按钮：课程页位于语言切换旁，评测页位于主题切换前
        lang = HTML.index('id="langSelector"')
        button = HTML.index('id="msgBoardBtn"')
        theme = HTML.index('onclick="toggleTheme()"')
        self.assertLess(lang, button)
        self.assertLess(button, theme)
        self.assertLess(
            REVIEWS_HTML.index('id="msgBoardBtn"'),
            REVIEWS_HTML.index('id="themeButton"'),
        )
        for page in (HTML, REVIEWS_HTML):
            # 悬浮面板：提示语、输入框、可滚动列表、加载更多
            self.assertIn('id="msgOverlay" role="dialog" aria-modal="true"', page)
            self.assertIn("欢迎公开留言：问题反馈、功能建议、想对开发者说的话都可以写在这里，所有人可见。", page)
            self.assertIn('maxlength="500"', page)
            self.assertIn(".msg-list { flex: 1; overflow-y: auto;", page)
            self.assertIn('id="msgMore"', page)
            # 留言内容只能通过 textContent 渲染，禁止拼入 innerHTML
            self.assertIn("body.textContent = message.content", page)
            self.assertIn("time.textContent = formatMsgTime(message.posted_at)", page)
            # 焦点与键盘契约
            self.assertIn("function trapMsgFocus", page)
            self.assertIn("closeMsgBoard()", page)
            self.assertIn('aria-label="关闭留言板"', page)

    def test_message_replies_nickname_and_changelog_contract(self):
        blocks = []
        for page in (HTML, REVIEWS_HTML):
            for fragment in (
                'id="msgChangelogButton"', 'aria-controls="msgChangelog"',
                'id="msgBoardView"', 'id="msgChangelog" role="region"',
                "function addMessageReplies", "form.hidden = true", "input.maxLength = 500",
                "message.nickname || DEFAULT_NICKNAME", "renderMessage(reply, true)",
                "before_id=${beforeId}", "if (!loaded) await loadReplies()",
                "body: JSON.stringify({ content })", "replies.hidden = !replies.hidden",
                "button.setAttribute('aria-expanded', String(changelog))",
                "document.getElementById('msgBoardView').hidden = changelog",
                "document.getElementById('msgChangelog').hidden = !changelog",
                "fetch('/api/changelog'", "返回留言板", "路过的 PKUer",
            ):
                self.assertIn(fragment, page)
            start = page.index('let msgReturnFocus = null;')
            end = page.index('function trapMsgFocus', start)
            block = page[start:end]
            inert_helper = re.search(r'  (set\w+BackgroundInert)\(true\);', block).group(1)
            self.assertIn(f'function {inert_helper}(', page)
            self.assertIn(f'{inert_helper}(false);', block)
            blocks.append(block.replace('setThreadModalBackgroundInert', 'setModalBackgroundInert'))
            self.assertNotIn('innerHTML', page[start:end])
        self.assertEqual(blocks[0], blocks[1])
        self.assertIn("nickname: buildChangeNicknameForm", HTML)
        self.assertIn("favApi('POST', '/api/account/nickname', { nickname })", HTML)
        self.assertIn("autocomplete: 'nickname', maxlength: '30'", HTML)
        self.assertIn('id="accountStatus" role="status"', HTML)

    def test_visit_stats_button_and_panel_contract(self):
        # 两页顶栏都有统计按钮，且排在留言按钮之前
        lang = HTML.index('id="langSelector"')
        button = HTML.index('id="statsBtn"')
        self.assertLess(lang, button)
        self.assertLess(button, HTML.index('id="msgBoardBtn"'))
        self.assertLess(
            REVIEWS_HTML.index('id="statsBtn"'),
            REVIEWS_HTML.index('id="msgBoardBtn"'),
        )
        for page in (HTML, REVIEWS_HTML):
            # 悬浮面板：三块统计、近 7 天趋势、隐私说明
            self.assertIn('id="statsOverlay" role="dialog" aria-modal="true"', page)
            for stat_id in (
                'id="statTodayViews"',
                'id="statWeekViews"',
                'id="statTotalViews"',
                'id="trendChart"',
            ):
                self.assertIn(stat_id, page)
            self.assertIn("按北京时间分日统计", page)
            # 数字与趋势只经 textContent / 数值高度渲染，禁止拼入 innerHTML
            self.assertIn("textContent = Number(value).toLocaleString", page)
            self.assertIn("count.textContent = point.views", page)
            self.assertIn("bar.style.height = Math.round", page)
            # 打开时轮询、关闭时清理定时器；焦点与键盘契约
            self.assertIn("statsTimer = setInterval(loadStats, 20000)", page)
            self.assertIn("clearInterval(statsTimer)", page)
            self.assertIn("function trapStatsFocus", page)
            self.assertIn('aria-label="关闭访问统计"', page)

    def test_sponsor_button_opens_in_page_panel_with_qr_codes_and_thanks(self):
        sponsor_list = re.search(r'<ul class="sponsor-list">.*?</ul>', HTML, flags=re.S).group(0)
        sponsors = re.findall(r'<li class="sponsor-chip">.*?</li>', sponsor_list, flags=re.S)
        self.assertGreaterEqual(len(sponsors), 3)
        for page in (HTML, REVIEWS_HTML):
            # 赞助按钮不再跳转 GitHub，而是打开站内悬浮面板
            self.assertNotIn("github.com/WishingCat/Pinhaoke#", page)
            self.assertIn(
                '<button class="about-link-btn about-sponsor" type="button" onclick="openSponsor()" aria-label="赞助">',
                page,
            )
            self.assertIn('id="sponsorOverlay" role="dialog" aria-modal="true"', page)
            panel = re.search(
                r'<div[^>]+id="sponsorOverlay".*?</ul>\s*</div>\s*</div>', page, flags=re.S
            ).group(0)
            outside_panel = page.replace(panel, "", 1)
            # 仅保留微信赞助方式，开发者联系二维码与完整鸣谢名单仍在面板中。
            for asset in (
                'src="/Images/wechat_sponsor.jpg?v=2" alt="微信赞助码"',
                'src="/Images/MyWeChat.jpg" alt="微信联系方式"',
            ):
                self.assertIn(asset, panel)
            for asset in ("wechat_sponsor.jpg", "alipay_sponsor.jpg"):
                self.assertNotIn(asset, outside_panel)
            self.assertNotIn("alipay_sponsor", page)
            self.assertNotIn("支付宝赞助码", page)
            self.assertLess(page.index('alt="微信赞助码"'), page.index('alt="微信联系方式"'))
            self.assertLess(page.index('alt="微信联系方式"'), page.index(">鸣谢赞助<"))
            # 两个网页的名单、金额与日期保持一致，且只在赞助面板内展示。
            self.assertIn(sponsor_list, panel)
            self.assertNotIn('class="sponsor-name"', outside_panel)
            self.assertNotIn('class="sponsor-amount"', outside_panel)
            self.assertEqual(page.count('class="sponsor-chip"'), len(sponsors))
            # 焦点与键盘契约；关闭后先钉住悬浮卡再把焦点还给赞助按钮
            self.assertIn("function trapSponsorFocus", page)
            self.assertIn("closeSponsor()", page)
            self.assertIn('aria-label="关闭赞助面板"', page)
            self.assertIn(".tip-wrap.tip-pinned .tip-card { display: block; }", page)
            self.assertIn("tipWrap.classList.add('tip-pinned')", page)
            self.assertIn("focusReturnTarget(returnTarget)", page)

    def test_developer_contact_is_consistent_in_about_panel_and_footer(self):
        self.assertEqual(HTML.count("VX 联系方式："), 1)
        self.assertIn("<dt>微信联系</dt>", HTML)
        self.assertEqual(HTML.count("tuzengji"), 2)
        self.assertEqual(HTML.count("如果有问题或需求 欢迎联系！"), 2)
        self.assertIn('class="about-details"', HTML)
        self.assertNotIn('class="about-avatar"', HTML)
        self.assertIn('class="footer-contact"', HTML)

    def test_about_panel_stays_open_and_allows_text_selection(self):
        self.assertIn('class="tip-wrap about-wrap" hidden>', HTML)
        self.assertIn(".about-wrap:hover::after", HTML)
        self.assertIn("pointer-events: auto;", HTML)
        self.assertIn("-webkit-user-select: text;", HTML)
        self.assertIn("user-select: text;", HTML)

    def test_review_page_uses_read_only_apis_and_safe_text_rendering(self):
        for endpoint in ("/api/reviews?", "/api/review-courses?", "/api/reviews/meta"):
            self.assertIn(endpoint, REVIEWS_HTML)
        self.assertIn("`/api/reviews/${encodeURIComponent(pid)}`", REVIEWS_HTML)
        self.assertIn("requestId !== state.requestId", REVIEWS_HTML)
        self.assertIn("textContent = text", REVIEWS_HTML)
        self.assertIn("function renderHighlightedText", REVIEWS_HTML)
        self.assertIn("document.createTextNode", REVIEWS_HTML)
        self.assertIn("Array.from(value)", REVIEWS_HTML)
        self.assertIn("entity-course", REVIEWS_HTML)
        self.assertIn("entity-teacher", REVIEWS_HTML)
        self.assertIn("function stableEntityColor", REVIEWS_HTML)
        self.assertIn("item.match_kind === 'alias'", REVIEWS_HTML)
        self.assertIn("entity-color-5", REVIEWS_HTML)
        self.assertIn("url.hostname === 'treehole.pku.edu.cn'", REVIEWS_HTML)
        self.assertNotIn("addToPlan.do", REVIEWS_HTML)

    def test_review_cards_use_full_tinted_borders_and_open_snapshot_modal(self):
        self.assertIn("border: 1.5px solid var(--border);", REVIEWS_HTML)
        self.assertIn(
            "border-color: color-mix(in srgb, var(--thread-c) 38%, var(--border));",
            REVIEWS_HTML,
        )
        for color in range(6):
            self.assertIn(f'.thread[data-color="{color}"]', REVIEWS_HTML)
        self.assertNotIn(".thread::before", REVIEWS_HTML)
        self.assertNotIn(".thread-review::before", REVIEWS_HTML)
        self.assertNotIn(".thread-discussion::before", REVIEWS_HTML)
        self.assertIn('role="dialog" aria-modal="true"', REVIEWS_HTML)
        self.assertIn('id="threadModalOverlay" hidden', REVIEWS_HTML)
        self.assertIn("openThreadModal(thread.pid, article)", REVIEWS_HTML)
        self.assertIn("event.target.closest('a, button')", REVIEWS_HTML)
        self.assertIn("setThreadModalBackgroundInert(true)", REVIEWS_HTML)
        self.assertIn("trapThreadModalFocus(event)", REVIEWS_HTML)
        self.assertIn("threadModalOverlay.hidden && returnTarget.isConnected", REVIEWS_HTML)
        self.assertIn("threadModalBody.replaceChildren(fragment)", REVIEWS_HTML)
        self.assertIn("快照内全部回复", REVIEWS_HTML)
        self.assertIn(".thread-modal-overlay { align-items: flex-end; padding: 12px; }", REVIEWS_HTML)

    def test_review_page_keeps_review_entry_separate_and_mobile_friendly(self):
        term_nav = REVIEWS_HTML.index('class="term-nav"')
        term_nav_end = REVIEWS_HTML.index('</div>', term_nav)
        review_shell = REVIEWS_HTML.index('class="review-nav-shell"')
        review_link = REVIEWS_HTML.index('class="review-nav-link"')
        self.assertLess(term_nav_end, review_shell)
        self.assertLess(term_nav_end, review_link)
        self.assertLess(review_shell, review_link)
        self.assertIn('.term-nav, .review-nav-shell {', REVIEWS_HTML)
        self.assertIn('.term-nav a { padding: 7px 18px; border-radius: 999px; }', REVIEWS_HTML)
        for page in (HTML, REVIEWS_HTML):
            self.assertIn('grid-template-columns: repeat(2, minmax(0, 1fr))', page)
            self.assertNotIn('flex-basis: 100%;', page)
        self.assertIn('.term-toggle { display: contents; }', HTML)
        self.assertIn('.term-nav, .review-nav-shell { display: contents; }', REVIEWS_HTML)

    def test_review_page_matches_index_visual_language(self):
        # 首页使用独立圆体字标；共享学期控件尺寸、暗色底色、
        # 激活态渐变、卡片/弹窗圆角、页脚与回到顶部
        for token in (
            "#0E1013",
            "linear-gradient(135deg, #08766B, #075F57)",
            "rgba(22, 123, 114, 0.35)",
        ):
            self.assertIn(token, REVIEWS_HTML)
            self.assertIn(token, HTML)
        self.assertIn("border-radius: 14px; background: var(--surface)", REVIEWS_HTML)
        self.assertIn('class="site-footer"', REVIEWS_HTML)
        self.assertIn('class="footer-contact"', REVIEWS_HTML)
        self.assertIn('id="backTop"', REVIEWS_HTML)
        self.assertIn("window.scrollY > 600", REVIEWS_HTML)
        # 顶栏右侧与课程页一致：主题、关于悬浮卡；GitHub 与赞助并列在关于卡头部下方
        self.assertIn('class="tip-wrap about-wrap" hidden>', REVIEWS_HTML)
        self.assertIn('class="tip-card about-tip"', REVIEWS_HTML)
        self.assertIn(".about-wrap:hover::after", REVIEWS_HTML)
        self.assertIn("user-select: text;", REVIEWS_HTML)
        for page in (HTML, REVIEWS_HTML):
            self.assertIn('class="about-links"', page)
            self.assertIn('class="about-link-btn about-sponsor"', page)
            self.assertIn('aria-label="GitHub"', page)
            self.assertNotIn('class="about-links-note">纯公益项目，承诺永久免费服务', page)
            self.assertIn("Zengji Tu", page)
            self.assertIn("Ningjing Wang", page)
            self.assertIn("Tingyi Huang", page)
            self.assertLess(
                page.index("纯公益项目 · 实时更新"),
                page.index('class="about-links"'),
            )
            # 赞助与 GitHub 已移出顶栏，赞助不再有悬浮文字
            self.assertNotIn("sponsor-btn", page)
            self.assertNotIn("点击跳转 GitHub 赞助页面", page)
        self.assertNotIn(">课程搜索</span>", REVIEWS_HTML)
        # 关于卡与页脚各保留一份联系方式
        self.assertEqual(REVIEWS_HTML.count("tuzengji"), 2)
        self.assertEqual(REVIEWS_HTML.count("如果有问题或需求 欢迎联系！"), 2)

    def test_review_page_shows_date_range_and_combined_review_count(self):
        # 统计信息以小字备注显示在结果标题右侧，不再是独立统计区
        self.assertNotIn('class="stat"', REVIEWS_HTML)
        self.assertNotIn('class="stats"', REVIEWS_HTML)
        self.assertIn('class="result-meta" id="stats"', REVIEWS_HTML)
        self.assertIn(".result-meta { margin: 0; color: var(--ink-3); font-size: 0.72rem; }", REVIEWS_HTML)
        self.assertIn("数据范围", REVIEWS_HTML)
        self.assertIn('id="statDateRange"', REVIEWS_HTML)
        self.assertIn("`${startDate} 至 ${endDate}`", REVIEWS_HTML)
        self.assertIn("评测数据量", REVIEWS_HTML)
        self.assertIn('id="statTotal"', REVIEWS_HTML)
        self.assertIn(
            "Number(meta.matched_threads) + Number(meta.matched_replies)",
            REVIEWS_HTML,
        )
        for removed_id in ("statThreads", "statReplies", "statCoverage"):
            self.assertNotIn(removed_id, REVIEWS_HTML)
        self.assertNotIn('id="resultCount"', REVIEWS_HTML)
        self.assertNotIn("个相关树洞", REVIEWS_HTML)
        self.assertNotIn(".result-count", REVIEWS_HTML)

    def test_review_page_uses_requested_source_copy_and_right_aligned_total(self):
        self.assertIn("数据全部来自北大树洞大家的真实回复", REVIEWS_HTML)
        self.assertIn('<p class="result-meta" id="stats" aria-label="数据范围">', REVIEWS_HTML)
        self.assertNotIn("搜索课程名，查看课程评测主帖及其中有实际评价信息的回复。", REVIEWS_HTML)

    def test_review_page_uses_distinct_two_color_ambient_glow(self):
        self.assertIn("radial-gradient(40% 62% at 14% 0%", REVIEWS_HTML)
        self.assertIn("radial-gradient(36% 58% at 88% 5%", REVIEWS_HTML)
        self.assertIn("radial-gradient(30% 50% at 60% 30%", REVIEWS_HTML)
        self.assertIn("#F2578F", REVIEWS_HTML)
        self.assertIn("#5C8FDB", REVIEWS_HTML)
        self.assertIn("#FF8FAB", REVIEWS_HTML)
        self.assertIn("#A7C5D8", REVIEWS_HTML)
        self.assertIn(".page-head::before", REVIEWS_HTML)
        self.assertIn("radial-gradient(45% 60% at 33% 42%", REVIEWS_HTML)
        self.assertIn("radial-gradient(40% 55% at 67% 46%", REVIEWS_HTML)
        self.assertIn("body::before { animation: none !important; }", REVIEWS_HTML)
        self.assertNotIn("linear-gradient(118deg, rgba(53, 198, 167, 0.10)", REVIEWS_HTML)
        # 页面环境光晕（body::before 浅色与深色两段）不得回退到课程页的薄荷绿/靛蓝
        ambient = REVIEWS_HTML[REVIEWS_HTML.index("body::before"):REVIEWS_HTML.index("::selection")]
        self.assertIn('html[data-theme="dark"] body::before', ambient)
        self.assertNotIn("rgba(53, 198, 167", ambient)
        self.assertNotIn("rgba(79, 70, 229", ambient)

    def test_review_search_placeholder_mentions_courses_and_teachers(self):
        self.assertIn(
            'placeholder="可以搜索课程、老师，例如 YQF，马原……"',
            REVIEWS_HTML,
        )

    def test_review_search_matches_course_search_and_gates_popular_courses(self):
        self.assertIn('class="search-controls"', REVIEWS_HTML)
        self.assertIn('class="search-form" id="searchForm"', REVIEWS_HTML)
        self.assertIn('padding: 14px 20px;', REVIEWS_HTML)
        self.assertIn('border-radius: 14px;', REVIEWS_HTML)
        self.assertIn('id="popularButton"', REVIEWS_HTML)
        self.assertIn('<span>热门课程</span>', REVIEWS_HTML)
        self.assertIn('aria-controls="popularCourses"', REVIEWS_HTML)
        self.assertIn("fetch('/api/review-courses?q=&limit=24'", REVIEWS_HTML)
        self.assertNotIn("查看热门课程", REVIEWS_HTML)
        self.assertIn("searchInput.addEventListener('input', scheduleSearch)", REVIEWS_HTML)
        self.assertIn("setTimeout(() => runSearch(searchInput.value), 300)", REVIEWS_HTML)
        self.assertNotIn('class="submit-search"', REVIEWS_HTML)
        self.assertNotIn('id="suggestions"', REVIEWS_HTML)
        self.assertNotIn('role="combobox"', REVIEWS_HTML)
        self.assertNotIn("searchInput.addEventListener('focus'", REVIEWS_HTML)

    def test_review_page_omits_archive_eyebrow(self):
        self.assertNotIn("PKU TREEHOLE ARCHIVE", REVIEWS_HTML)
        self.assertNotIn('class="eyebrow"', REVIEWS_HTML)
        self.assertNotIn(".eyebrow {", REVIEWS_HTML)

    def test_load_more_guards_before_next_page_and_commits_after_success(self):
        body = function_body("loadMore")
        guard = "if (isLoading || !hasMore) return"
        self.assertIn(guard, body)
        self.assertLess(body.index(guard), body.index("currentPage + 1"))
        self.assertIn("await fetchCourses({ page: nextPage, append: true })", body)
        self.assertIn("if (appended) currentPage = nextPage", body)
        self.assertIn("loadMoreBtn.disabled = true", body)
        self.assertIn("finally", body)

    def test_fetch_courses_has_explicit_page_contract_and_boolean_result(self):
        self.assertRegex(
            HTML,
            r"async function fetchCourses\s*\(\s*\{\s*page\s*=\s*1\s*,\s*append\s*=\s*false\s*\}\s*=\s*\{\}\s*\)",
        )
        body = function_body("fetchCourses")
        self.assertIn("return true", body)
        self.assertIn("return false", body)
        self.assertNotIn("currentPage++", body)

    def test_fetch_courses_owns_its_term_controller_and_request_params(self):
        body = function_body("fetchCourses")
        self.assertIn("const requestedTerm = currentTerm", body)
        self.assertIn("params.set('term', requestedTerm)", body)
        self.assertIn("fetchController === ctrl", body)
        self.assertIn("ctrl.signal.aborted", body)
        self.assertIn("currentTerm === requestedTerm", body)

    def test_stale_course_success_cannot_commit_or_append(self):
        source = function_source("fetchCourses")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            let currentTerm = 'spring';
            let currentLang = 'zh';
            let fetchController = null;
            let isFetching = false;
            let currentCourses = [{{ id: 'keep' }}];
            let totalCount = 1;
            let hasMore = true;
            let currentPage = 4;
            let randomSeed = 17;
            const PAGE_SIZE = 20;
            const csValues = {{}};
            const inputs = {{
              searchInput: {{ value: '' }},
              filterClassroom: {{ value: '' }},
            }};
            const document = {{ getElementById: id => inputs[id] }};
            let requestedUrl = '';
            let releaseFetch;
            let renderCardsCalls = 0;
            let appendCardsCalls = 0;
            let updateCalls = 0;
            let renderErrorCalls = 0;
            function fetch(url) {{
              requestedUrl = url;
              return new Promise(resolve => {{ releaseFetch = resolve; }});
            }}
            function clearLoadMoreError() {{}}
            function renderSkeletons() {{}}
            function syncURL() {{}}
            function renderCards() {{ renderCardsCalls += 1; }}
            function appendCards() {{ appendCardsCalls += 1; }}
            function updateLoadMoreState() {{ updateCalls += 1; }}
            function renderError() {{ renderErrorCalls += 1; }}
            {source}
            (async () => {{
              const stale = fetchCourses({{ page: 5, append: true }});
              const oldController = fetchController;
              currentTerm = 'fall';
              oldController.abort();
              assert.equal(fetchController, oldController);
              releaseFetch({{
                ok: true,
                json: async () => ({{ total: 99, courses: [{{ id: 'old' }}] }}),
              }});
              assert.equal(await stale, false);
              assert.equal(new URL(requestedUrl, 'http://local').searchParams.get('term'), 'spring');
              assert.deepEqual(currentCourses, [{{ id: 'keep' }}]);
              assert.equal(totalCount, 1);
              assert.equal(hasMore, true);
              assert.equal(currentPage, 4);
              assert.equal(renderCardsCalls, 0);
              assert.equal(appendCardsCalls, 0);
              assert.equal(updateCalls, 0);
              assert.equal(renderErrorCalls, 0);
            }})().catch(error => {{ globalThis.console.error(error); process.exitCode = 1; }});
            """
        )

    def test_stale_course_http_and_json_errors_are_suppressed(self):
        source = function_source("fetchCourses")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            const realConsole = globalThis.console;
            let loggedErrors = 0;
            const console = {{ error() {{ loggedErrors += 1; }} }};
            let currentTerm = 'spring';
            let currentLang = 'zh';
            let fetchController = null;
            let isFetching = false;
            let currentCourses = [{{ id: 'keep' }}];
            let totalCount = 1;
            let hasMore = true;
            let randomSeed = 17;
            const PAGE_SIZE = 20;
            const csValues = {{}};
            const inputs = {{
              searchInput: {{ value: '' }},
              filterClassroom: {{ value: '' }},
            }};
            const document = {{ getElementById: id => inputs[id] }};
            let scenario = 'http';
            let releaseHttp;
            let rejectJson;
            let markJsonStarted;
            const jsonStarted = new Promise(resolve => {{ markJsonStarted = resolve; }});
            let renderCardsCalls = 0;
            let appendCardsCalls = 0;
            let updateCalls = 0;
            let renderErrorCalls = 0;
            function fetch() {{
              if (scenario === 'http') {{
                return new Promise(resolve => {{ releaseHttp = resolve; }});
              }}
              return Promise.resolve({{
                ok: true,
                json: () => {{
                  markJsonStarted();
                  return new Promise((_resolve, reject) => {{ rejectJson = reject; }});
                }},
              }});
            }}
            function clearLoadMoreError() {{}}
            function renderSkeletons() {{}}
            function syncURL() {{}}
            function renderCards() {{ renderCardsCalls += 1; }}
            function appendCards() {{ appendCardsCalls += 1; }}
            function updateLoadMoreState() {{ updateCalls += 1; }}
            function renderError() {{ renderErrorCalls += 1; }}
            {source}
            (async () => {{
              const staleHttp = fetchCourses();
              const httpController = fetchController;
              currentTerm = 'fall';
              httpController.abort();
              assert.equal(fetchController, httpController);
              releaseHttp({{ ok: false, status: 503 }});
              assert.equal(await staleHttp, false);

              scenario = 'json';
              currentTerm = 'spring';
              const staleJson = fetchCourses();
              await jsonStarted;
              const jsonController = fetchController;
              currentTerm = 'fall';
              jsonController.abort();
              assert.equal(fetchController, jsonController);
              rejectJson(new Error('stale JSON failure'));
              assert.equal(await staleJson, false);

              assert.deepEqual(currentCourses, [{{ id: 'keep' }}]);
              assert.equal(totalCount, 1);
              assert.equal(hasMore, true);
              assert.equal(renderCardsCalls, 0);
              assert.equal(appendCardsCalls, 0);
              assert.equal(updateCalls, 0);
              assert.equal(renderErrorCalls, 0);
              assert.equal(loggedErrors, 0);
            }})().catch(error => {{ realConsole.error(error); process.exitCode = 1; }});
            """
        )

    def test_rapid_load_more_calls_execute_one_append_and_one_page_advance(self):
        source = function_source("loadMore")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            let currentPage = 1;
            let isLoading = false;
            let hasMore = true;
            let appendCalls = 0;
            let finishAppend;
            const loadMoreBtn = {{ disabled: false }};
            const document = {{ getElementById: id => loadMoreBtn }};
            function fetchCourses(args) {{
              appendCalls += 1;
              assert.deepEqual(args, {{ page: 2, append: true }});
              return new Promise(resolve => {{ finishAppend = resolve; }});
            }}
            {source}
            (async () => {{
              const calls = [loadMore(), loadMore(), loadMore()];
              assert.equal(appendCalls, 1);
              assert.equal(currentPage, 1);
              assert.equal(loadMoreBtn.disabled, true);
              finishAppend(true);
              await Promise.all(calls);
              assert.equal(currentPage, 2);
              assert.equal(appendCalls, 1);
              assert.equal(isLoading, false);
              assert.equal(loadMoreBtn.disabled, false);
            }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
            """
        )

    def test_delayed_filter_response_cannot_overwrite_active_term_state(self):
        source = function_source("loadFiltersForCurrentTerm")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            let currentTerm = 'spring';
            let cachedFilters = null;
            let filtersController = null;
            const filtersByTerm = {{}};
            const pending = {{}};
            const populated = [];
            function populateFilters(data) {{ populated.push(data.term); }}
            function fetch(url, options) {{
              const term = new URL(url, 'http://local').searchParams.get('term');
              return new Promise(resolve => {{
                pending[term] = data => resolve({{ ok: true, json: async () => data }});
              }});
            }}
            {source}
            (async () => {{
              const spring = loadFiltersForCurrentTerm();
              currentTerm = 'fall';
              const fall = loadFiltersForCurrentTerm();
              pending.fall({{ term: 'fall' }});
              const fallResult = await fall;
              pending.spring({{ term: 'spring' }});
              const springResult = await spring;
              assert.equal(fallResult, true);
              assert.equal(springResult, false);
              assert.deepEqual(Object.keys(filtersByTerm), ['fall']);
              assert.equal(filtersByTerm.fall.term, 'fall');
              assert.equal(cachedFilters.term, 'fall');
              assert.deepEqual(populated, ['fall']);
            }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
            """
        )

    def test_filter_loader_has_request_owned_term_and_controller_contract(self):
        body = function_body("loadFiltersForCurrentTerm")
        self.assertIn("const requestedTerm = currentTerm", body)
        self.assertIn("const controller = new AbortController()", body)
        self.assertIn("filtersController === controller", body)
        self.assertIn("currentTerm === requestedTerm", body)
        self.assertIn("return true", body)
        self.assertIn("return false", body)

    def test_stale_filter_http_and_json_failures_resolve_false(self):
        source = function_source("loadFiltersForCurrentTerm")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            let currentTerm = 'spring';
            let cachedFilters = null;
            let filtersController = null;
            const filtersByTerm = {{}};
            let scenario = 'http';
            let resolveSpringHttp;
            let rejectSpringJson;
            let markJsonStarted;
            const jsonStarted = new Promise(resolve => {{ markJsonStarted = resolve; }});
            function populateFilters(data) {{ cachedFilters = data; }}
            function fetch(url) {{
              const term = new URL(url, 'http://local').searchParams.get('term');
              if (term === 'fall') return Promise.resolve({{ ok: true, json: async () => ({{ term: 'fall' }}) }});
              if (scenario === 'http') {{
                return new Promise(resolve => {{ resolveSpringHttp = resolve; }});
              }}
              return Promise.resolve({{
                ok: true,
                json: () => {{
                  markJsonStarted();
                  return new Promise((_resolve, reject) => {{ rejectSpringJson = reject; }});
                }}
              }});
            }}
            {source}
            (async () => {{
              const staleHttp = loadFiltersForCurrentTerm();
              currentTerm = 'fall';
              const activeFall = loadFiltersForCurrentTerm();
              assert.equal(await activeFall, true);
              resolveSpringHttp({{ ok: false, status: 503, json: async () => {{ throw new Error('unused'); }} }});
              assert.equal(await staleHttp, false);

              for (const key of Object.keys(filtersByTerm)) delete filtersByTerm[key];
              filtersController = null;
              cachedFilters = null;
              scenario = 'json';
              currentTerm = 'spring';
              const staleJson = loadFiltersForCurrentTerm();
              await jsonStarted;
              currentTerm = 'fall';
              assert.equal(await loadFiltersForCurrentTerm(), true);
              rejectSpringJson(new Error('bad stale json'));
              assert.equal(await staleJson, false);
              assert.equal(cachedFilters.term, 'fall');
              assert.deepEqual(Object.keys(filtersByTerm), ['fall']);
            }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
            """
        )

    def test_stale_init_and_set_term_callers_stop_all_continuations(self):
        init_source = function_source("init")
        set_term_source = function_source("setTerm")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            let currentTerm = 'fall';
            let currentModalCourseId = null;
            let fetchController = null;
            let isFetching = false;
            let isLoading = false;
            let hasMore = false;
            let currentPage = 1;
            const TERMS = new Set(['fall', 'spring', 'summer']);
            const csValues = {{}};
            const counts = {{ fetch: 0, error: 0, chips: 0, i18n: 0, detail: 0 }};
            let finishSetTermLoad;
            let loaderMode = 'init';
            const classList = {{ toggle() {{}}, add() {{}}, remove() {{}} }};
            const element = {{ value: '', classList, observe() {{}} }};
            const document = {{ getElementById: () => element, querySelectorAll: () => [] }};
            const window = {{ addEventListener() {{}} }};
            function readURLState() {{ return 'a1'; }}
            function initFavorites() {{}}
            function initTermIndicator() {{}}
            function refreshTermToggleUI() {{}}
            function refreshLangSelectorUI() {{}}
            function applyI18n() {{ counts.i18n += 1; }}
            function updateResultsCount() {{}}
            function renderSkeletons() {{}}
            function applyThemeMeta() {{}}
            function renderChips() {{ counts.chips += 1; }}
            function renderError() {{ counts.error += 1; }}
            function showDetail() {{ counts.detail += 1; }}
            function closeModal() {{}}
            function fetchCourses() {{
              counts.fetch += 1;
              if (fetchController) fetchController.abort();
              return Promise.resolve(true);
            }}
            function loadMore() {{}}
            function loadFiltersForCurrentTerm() {{
              if (loaderMode === 'init') return Promise.resolve(false);
              return new Promise(resolve => {{ finishSetTermLoad = resolve; }});
            }}
            {init_source}
            {set_term_source}
            (async () => {{
              await init();
              assert.deepEqual(counts, {{ fetch: 0, error: 0, chips: 0, i18n: 1, detail: 0 }});

              loaderMode = 'setTerm';
              const staleSetTerm = setTerm('spring');
              currentTerm = 'fall';
              fetchController = {{ abort: () => {{ throw new Error('stale caller aborted active list'); }} }};
              finishSetTermLoad(false);
              assert.equal(await staleSetTerm, false);
              assert.equal(counts.fetch, 0);
              assert.equal(counts.error, 0);
              assert.equal(counts.i18n, 1);
            }})().catch(error => {{ console.error(error); process.exitCode = 1; }});
            """
        )

    def test_init_and_set_term_structurally_gate_filter_ownership(self):
        for name in ("init", "setTerm"):
            with self.subTest(function=name):
                body = function_body(name)
                self.assertIn("const filtersLoaded = await loadFiltersForCurrentTerm()", body)
                self.assertIn("if (!filtersLoaded", body)

    def test_course_actions_and_personal_title_match_mobile_layout(self):
        self.assertNotIn('copyCourseLink', HTML)
        self.assertNotIn('copyLinkBtn', HTML)
        detail = function_body('showDetail')
        self.assertIn('class="modal-head course-detail-head"', detail)
        self.assertIn('class="course-save-actions"', detail)
        self.assertIn('.course-save-actions .modal-action span { display: inline; }', HTML)
        self.assertIn('id="favTitle">个人中心', HTML)
        self.assertIn('body.appendChild(seg)', function_body('renderFavAuth'))

    def test_sync_url_executes_complete_state_and_omits_default_fall(self):
        source = function_source("syncURL")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            const values = {{ searchInput: 'optics', filterClassroom: '二教511' }};
            const document = {{
              getElementById: id => ({{ value: values[id] || '' }})
            }};
            const csValues = {{
              filterCourseType: '专业课',
              filterCategory: '任选',
              filterCredits: '2',
              filterDepartment: '物理学院',
              filterWeekday: '周一',
              filterPeriod: '3-4',
              filterGrading: '百分制',
              filterSort: 'random',
            }};
            let randomSeed = 731;
            let currentTerm = 'spring';
            let currentLang = 'en';
            let currentModalCourseId = 'u42';
            const location = {{ pathname: '/courses' }};
            let replaced = '';
            const history = {{ replaceState: (_state, _title, url) => {{ replaced = url; }} }};
            {source}
            syncURL();
            let params = new URL(replaced, 'http://local').searchParams;
            assert.deepEqual(Object.fromEntries(params), {{
              q: 'optics', room: '二教511', type: '专业课', cat: '任选', credits: '2',
              dept: '物理学院', day: '周一', period: '3-4', grading: '百分制', sort: 'random',
              seed: '731', term: 'spring', lang: 'en', course: 'u42'
            }});
            currentTerm = 'fall';
            syncURL();
            params = new URL(replaced, 'http://local').searchParams;
            assert.equal(params.has('term'), false);
            """
        )

    def test_weekday_label_and_period_filter_contract(self):
        self.assertIn('<label id="labelWeekday">星期几</label>', HTML)
        self.assertIn('<label id="labelPeriod">上课节时</label>', HTML)
        self.assertIn('<div class="custom-select" id="csPeriod" data-filter="filterPeriod"></div>', HTML)
        self.assertLess(HTML.index('id="csWeekday"'), HTML.index('id="csPeriod"'))
        self.assertLess(HTML.index('id="csPeriod"'), HTML.index('id="filterClassroom"'))
        self.assertNotIn("labelWeekday: '上课时间'", HTML)
        self.assertIn("labelWeekday: '星期几', labelPeriod: '上课节时'", HTML)
        self.assertEqual(HTML.count("labelPeriod:"), 8)
        self.assertEqual(HTML.count("periodLabel: (a, b) =>"), 8)
        self.assertIn("#labelPeriod { --fl-icon:", HTML)
        self.assertIn("csValues.filterPeriod     = p.get('period') || '';", HTML)
        self.assertIn("csValues.filterPeriodMode = p.get('period_mode') === 'within' ? 'within' : '';", HTML)
        self.assertIn("p.set('period', csValues.filterPeriod)", HTML)
        self.assertIn("p.set('period_mode', 'within')", HTML)
        self.assertIn("params.set('period', csValues.filterPeriod)", HTML)
        self.assertIn("params.set('period_mode', 'within')", HTML)
        self.assertIn("buildWeekdaySelect('csWeekday', csValues.filterWeekday);", HTML)
        self.assertIn("buildPeriodSelect('csPeriod'", HTML)
        self.assertIn("(filters.periods || []).map(p => ({ value: p, label: fmtPeriod(p) }))", HTML)
        self.assertIn("'filterDepartment', 'filterWeekday', 'filterPeriod', 'filterPeriodMode', 'filterGrading'", HTML)
        self.assertIn("document.getElementById('labelPeriod').textContent = lang.labelPeriod;", HTML)
        self.assertIn("items.push({ k: 'filterWeekday', label: fmtWeekday(csValues.filterWeekday) })", HTML)
        self.assertIn("items.push({ k: 'filterPeriod', label: fmtPeriod(csValues.filterPeriod) })", HTML)
        self.assertIn("items.push({ k: 'filterPeriodMode', label: '仅在所选时段内' })", HTML)
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            const WEEKDAYS_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
            let currentLang = 'zh';
            const i18n = {{
              zh: {{
                weekdays: WEEKDAYS_CN,
                periodLabel: (a, b) => a === b ? `第${{a}}节` : `${{a}}-${{b}}节`,
              }},
              en: {{
                weekdays: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
                periodLabel: (a, b) => a === b ? `Period ${{a}}` : `Periods ${{a}}-${{b}}`,
              }},
            }};
            {function_source("parseWeekdaySelection")}
            {function_source("serializeWeekdays")}
            {function_source("fmtWeekday")}
            {function_source("parsePeriodSelection")}
            {function_source("serializePeriods")}
            {function_source("fmtPeriod")}
            assert.equal(fmtPeriod('5'), '第5节');
            assert.equal(fmtPeriod('10-11'), '10-11节');
            assert.equal(fmtPeriod('7-7'), '第7节');
            assert.equal(fmtPeriod('1-2,5-6'), '1-2、5-6节');
            assert.equal(fmtPeriod('1,3,5'), '第1、3、5节');
            assert.equal(fmtPeriod('bogus'), 'bogus');
            assert.equal(fmtPeriod(null), '');
            assert.equal(serializePeriods(parsePeriodSelection('6,5,2,1,8')), '1-2,5-6,8');
            assert.equal(serializeWeekdays(parseWeekdaySelection('周四,周二')), '周二,周四');
            assert.equal(fmtWeekday('周二,周四'), '周二、周四');
            assert.equal(fmtWeekday('周一,周二,周三,周四,周五'), '周一至周五');
            assert.equal(fmtWeekday('周六,周日'), '周六、周日');
            assert.equal(fmtWeekday('周一,周三,周五'), '周一/三/五');
            currentLang = 'en';
            assert.equal(fmtPeriod('3-4'), 'Periods 3-4');
            assert.equal(fmtPeriod('1-2,5'), 'Periods 1-2, Period 5');
            """
        )

    def test_sort_copy_no_longer_claims_pinyin(self):
        self.assertIn("sortNameAsc", HTML)
        self.assertIn("sortNameDesc", HTML)
        self.assertIn("value: 'name_asc'", HTML)
        self.assertIn("value: 'name_desc'", HTML)
        self.assertNotIn("sortPinyin", HTML)

    def test_detail_renders_book_fields_and_distinguishes_intro_labels(self):
        self.assertIn("c.textbook", HTML)
        self.assertIn("c.reference_book", HTML)
        self.assertIn("modalTextbook", HTML)
        self.assertIn("modalReferenceBook", HTML)
        self.assertIn("modalIntroTranslated", HTML)

    def test_course_field_helpers_preserve_exact_source_text(self):
        self.run_node(f"""
            const assert = require('node:assert/strict');
            let currentLang = 'en';
            {function_source('tr')}
            {function_source('trTeacher')}
            {function_source('trSchedule')}
            const schedule = '1~16周每周周二3~4节理教3189~16周单周周三5~6节';
            assert.equal(trSchedule(schedule), schedule);
            assert.equal(trTeacher('教师（教授）'), '教师（教授）');
            assert.equal(tr('departments', '数学科学学院'), '数学科学学院');
        """)

    def test_custom_select_exposes_keyboard_and_aria_contract(self):
        builder = function_body("buildCustomSelect")
        self.assertIn('role="combobox"', builder)
        self.assertIn('aria-haspopup="listbox"', builder)
        self.assertIn('aria-expanded="false"', builder)
        self.assertIn('role="listbox"', builder)
        self.assertIn('role="option"', builder)
        self.assertIn('aria-selected=', builder)
        self.assertIn('aria-controls=', builder)
        self.assertIn("ArrowDown", builder)
        self.assertIn("ArrowUp", builder)
        self.assertIn("Home", builder)
        self.assertIn("End", builder)
        self.assertIn("Escape", builder)
        self.assertIn("focusout", builder)

    def test_custom_select_focus_leave_closes_but_internal_focus_does_not(self):
        if not NODE:
            self.skipTest("node is unavailable; JavaScript behavior contract skipped")
        source = function_source("handleCustomSelectFocusOut")
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            const internal = {{ name: 'internal' }};
            const external = {{ name: 'external' }};
            const trigger = {{ expanded: 'true', setAttribute: (_key, value) => {{ trigger.expanded = value; }} }};
            const container = {{
              contains: element => element === internal,
              querySelector: () => trigger,
              classList: {{ remove: () => {{}} }},
            }};
            const document = {{ activeElement: internal }};
            const requestAnimationFrame = callback => callback();
            let closes = 0;
            function closeCustomSelect(target, restoreFocus) {{
              closes += 1;
              assert.equal(target, container);
              assert.equal(Boolean(restoreFocus), false);
              trigger.setAttribute('aria-expanded', 'false');
            }}
            {source}
            handleCustomSelectFocusOut(container, {{ relatedTarget: internal }});
            assert.equal(closes, 0);
            assert.equal(trigger.expanded, 'true');
            document.activeElement = external;
            handleCustomSelectFocusOut(container, {{ relatedTarget: external }});
            assert.equal(closes, 1);
            assert.equal(trigger.expanded, 'false');
            """
        )

    def test_modal_has_dialog_semantics_and_focus_management(self):
        self.assertIn('role="dialog"', HTML)
        self.assertIn('aria-modal="true"', HTML)
        self.assertIn("aria-labelledby=", HTML)
        self.assertIn("modalReturnFocus", HTML)
        self.assertIn("trapModalFocus", HTML)
        self.assertIn(".inert = true", HTML)
        self.assertIn(".inert = false", HTML)
        self.assertIn("aria-label=", function_body("showDetail"))

    def test_term_order_and_mobile_containment_contract(self):
        spring = HTML.index('id="termSpringBtn"')
        summer = HTML.index('id="termSummerBtn"')
        fall = HTML.index('id="termFallBtn"')
        self.assertLess(spring, summer)
        self.assertLess(summer, fall)
        self.assertIn("@media (max-width: 390px)", HTML)
        self.assertIn("@media (max-width: 320px)", HTML)
        self.assertRegex(HTML, r"\.term-toggle\s*\{[^}]*flex-wrap:\s*wrap")

    def test_mobile_filter_toggle_is_centered_wide_and_prominent(self):
        match = re.search(
            r"@media \(max-width: 720px\) \{\s*\.filters-toggle \{([^}]*)\}",
            HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        rules = match.group(1)
        self.assertIn("display: flex", rules)
        self.assertIn("justify-content: center", rules)
        self.assertIn("width: 100%", rules)
        self.assertIn("min-height: 50px", rules)
        self.assertIn("margin: 0 auto 14px", rules)
        self.assertIn("padding: 12px 16px", rules)
        self.assertIn("border: 1px solid var(--border)", rules)
        self.assertIn("border-radius: var(--r-lg)", rules)
        self.assertIn("background: var(--surface)", rules)
        self.assertNotIn("var(--accent)", rules)
        self.assertNotIn("gradient", rules)
        self.assertIn("font-size: 0.94rem", rules)
        self.assertIn("font-weight: 700", rules)
        self.assertIn(".filters-toggle:focus-visible {", HTML)
        self.assertIn("border-color: var(--border-2);", HTML)
        self.assertIn("color-mix(in srgb, var(--ink) 8%, transparent)", HTML)
        self.assertRegex(HTML, r"\.container\s*\{[^}]*min-width:\s*0")

    def test_mobile_filter_grid_keeps_at_least_two_columns(self):
        self.assertIn(
            ".filter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }",
            HTML,
        )
        self.assertIn(".filter-group { position: relative; min-width: 0; }", HTML)
        self.assertNotIn(
            "@media (max-width: 400px) { .filter-grid { grid-template-columns: 1fr; } }",
            HTML,
        )

    def test_privacy_and_dead_font_cleanup(self):
        self.assertNotIn("HarmonyOS Sans SC", HTML)
        self.assertNotIn("harmonyos-sans-font", HTML)
        self.assertIn("hm.src", HTML)
        self.assertIn("_hmt.push(['_setAutoPageview', false])", HTML)
        self.assertIn("_hmt.push(['_trackPageview', location.pathname])", HTML)

    def test_favorites_button_panel_and_account_contract(self):
        # 课程页顶栏：语言切换 < 我的收藏 < 访问统计；评测页不提供收藏
        lang = HTML.index('id="langSelector"')
        fav = HTML.index('id="favBtn"')
        self.assertLess(lang, fav)
        self.assertLess(fav, HTML.index('id="statsBtn"'))
        self.assertNotIn('id="favBtn"', REVIEWS_HTML)
        self.assertNotIn("favOverlay", REVIEWS_HTML)
        # 面板语义、状态区与滚动容器
        self.assertIn('id="favOverlay" role="dialog" aria-modal="true" aria-labelledby="favTitle"', HTML)
        self.assertIn('aria-label="关闭账号面板"', HTML)
        self.assertIn('id="favStatus" role="status" aria-live="polite"', HTML)
        self.assertIn(".fav-body { flex: 1; min-height: 0; overflow-y: auto;", HTML)
        self.assertIn("function trapFavFocus", HTML)
        self.assertIn("closeFavorites()", HTML)
        keydown = HTML[HTML.index("document.addEventListener('keydown'"):]
        self.assertLess(keydown.index("favOverlay"), keydown.index("sponsorOverlay"))
        self.assertIn("if (favOpen) {", keydown)
        # 面板内容只用 DOM API 与 textContent 渲染
        self.assertIn("node.textContent = text", function_body("favEl"))
        for name in (
            "renderFavoritesPanel", "renderFavAuth", "renderFavAccount", "renderFavList",
            "buildLoginForm", "buildRegisterForm", "buildResetForm", "buildChangePasswordForm",
            "buildChangeQuestionsForm", "buildDeleteForm", "setFavStatus", "favToast",
        ):
            self.assertNotIn("innerHTML", function_body(name), name)
        item = function_body("buildFavItem")
        self.assertEqual(item.count("innerHTML"), 1)
        self.assertIn("remove.innerHTML = ICONS.close", item)
        self.assertIn("createCard(item", item)
        # 卡片星标与详情弹窗按钮
        card = function_body("createCard")
        self.assertIn('<button class="fav-btn" type="button" aria-pressed="false"></button>', card)
        self.assertIn("favBtn.dataset.key = favoriteKey(course)", card)
        self.assertEqual(card.count("e.stopPropagation()"), 2)
        detail = function_body("showDetail")
        self.assertIn('id="favToggleBtn"', detail)
        self.assertIn('data-key="${esc(favoriteKey(c))}"', detail)
        self.assertIn("toggleFavorite(c, favToggle)", detail)
        self.assertIn("refreshFavoriteButtons();", function_body("applyI18n"))
        self.assertIn("initFavorites();", function_body("init"))
        # 八种语言的按钮文案
        for key in ("favAdd:", "favRemove:", "myFavorites:"):
            self.assertEqual(HTML.count(key), 8, key)
        # 表单语义与请求方式
        for token in (
            "autocomplete: 'username'", "autocomplete: 'current-password'",
            "autocomplete: 'new-password'", "minlength: '8'", "maxlength: '128'",
            "pattern: FAV_USERNAME_PATTERN", "FAV_USERNAME_PATTERN = '[A-Za-z0-9_]{3,20}'",
        ):
            self.assertIn(token, HTML)
        api = function_body("favApi")
        self.assertIn("credentials: 'same-origin'", api)
        self.assertIn("cache: 'no-store'", api)
        self.assertIn("pinhaoke_fav_mode", HTML)
        self.assertIn("favModeGet() !== 'user'", function_body("initFavorites"))
        self.assertNotIn("fav", function_body("syncURL").lower())
        self.assertNotIn("fav", function_body("readURLState").lower())
        # 必须登录才能收藏：未登录只记录待收藏课程并打开面板，不落 localStorage
        toggle = function_body("toggleFavorite")
        self.assertIn("if (!favUser) {", toggle)
        self.assertIn("pendingFavoriteId = course.id", toggle)
        self.assertIn("openFavorites()", toggle)
        self.assertNotIn("localStorage", toggle)
        self.assertIn("pendingFavoriteId", function_body("afterFavAuth"))
        # 成功登录或注册后重绘面板前复位忙碌标记，退出后仍可再次登录
        self.assertIn("favBusy = false;", function_body("renderFavoritesPanel"))
        self.assertEqual(HTML.count("favSetBusy(form, false);\n    await afterFavAuth("), 2)
        # 叠在课程详情之上时冻结详情弹窗，关闭时恢复
        self.assertIn("favStackedOnModal = courseModal.classList.contains('open')", function_body("openFavorites"))
        self.assertIn("courseModal.inert = true", function_body("openFavorites"))
        self.assertIn("courseModal.inert = false", function_body("closeFavorites"))
        # 星标为实体表面，不使用渐变；主按钮实色
        self.assertNotIn("gradient", HTML[HTML.index(".fav-btn {"):HTML.index(".fav-toast {")])

        # ---- 登录 / 个人双态与个人中心全屏视图 ----
        for key in ("favLogin:", "favAccount:"):
            self.assertEqual(HTML.count(key), 8, key)
        self.assertIn('onclick="openPersonal()"', HTML)
        self.assertIn('id="favBtnLabel"', HTML)
        personal = function_body("openPersonal")
        self.assertIn("openAccountView()", personal)
        self.assertIn("openFavorites()", personal)
        rfb = function_body("refreshFavoriteButtons")
        self.assertIn("favUser ? lang.favAccount : lang.favLogin", rfb)
        # 个人中心视图语义
        self.assertIn('id="accountView" role="dialog" aria-modal="true" aria-labelledby="accountViewTitle"', HTML)
        self.assertIn('id="accountBody"', HTML)
        self.assertIn('id="accountBack"', HTML)
        self.assertIn(".account-view { display: none; position: fixed; inset: 0; z-index: 1100;", HTML)
        opener = function_body("openAccountView")
        self.assertIn("setModalBackgroundInert(true)", opener)
        self.assertIn("history.pushState({ phk: 'account' }", opener)
        self.assertIn("window.addEventListener('popstate'", HTML)
        self.assertIn("function trapAccountFocus", HTML)
        # keydown：个人视图分支在 favOverlay 之前
        keydown_new = HTML[HTML.index("document.addEventListener('keydown'"):]
        self.assertIn("if (accountViewOpen) {", keydown_new)
        self.assertLess(keydown_new.index("accountViewOpen"), keydown_new.index("favOverlay"))
        # 新渲染函数只用 DOM API，唯一 innerHTML 在 favIcon
        for name in (
            "applyFavData", "renderFavViews", "openPersonal", "openAccountView",
            "closeAccountView", "trapAccountFocus", "renderAccountView", "renderCollections",
            "buildCollectionCard", "startRenameCollection", "confirmRemoveCollection",
            "renderCollectionChooser", "buildCollectionChip", "refreshDetailChooser",
            "scheduleSetCollections", "createCollection", "renameCollection", "removeCollection",
        ):
            self.assertNotIn("innerHTML", function_body(name), name)
        icon = function_body("favIcon")
        self.assertEqual(icon.count("innerHTML"), 1)
        self.assertIn("span.innerHTML = ICONS[key]", icon)
        # 详情弹窗就地选夹
        detail2 = function_body("showDetail")
        self.assertIn('id="favCollections"', detail2)
        self.assertIn("refreshDetailChooser();", detail2)
        chip = function_body("buildCollectionChip")
        self.assertIn("scheduleSetCollections(item.fav_key)", chip)
        sched = function_body("scheduleSetCollections")
        self.assertIn("/api/favorites/set-collections", sched)
        self.assertIn("}, 350)", sched)
        # 收藏夹 CRUD 走对应端点
        self.assertIn("/api/collections", function_body("createCollection"))
        self.assertIn("/api/collections/rename", function_body("renameCollection"))
        self.assertIn("/api/collections/remove", function_body("removeCollection"))
        # 收藏与账号状态不进入 URL
        for token in ("account", "collection"):
            self.assertNotIn(token, function_body("syncURL").lower())
            self.assertNotIn(token, function_body("readURLState").lower())
        # 从个人视图打开某条收藏
        self.assertIn("if (accountViewOpen) {", function_body("openFavoriteItem"))

        # ---- 任务一：个人中心内浮层看课（详情叠在个人视图之上，不跳首页）----
        ofi = function_body("openFavoriteItem")
        self.assertIn("showDetail(item.id, { overAccount: true })", ofi)
        self.assertNotIn("closeAccountView", ofi)  # 不再关闭个人视图
        self.assertIn(".modal-overlay.modal-over-account { z-index: 1200; }", HTML)
        detail3 = function_body("showDetail")
        self.assertIn("opts.overAccount", detail3)
        self.assertIn("modalStackedOnAccount = true", detail3)
        self.assertIn("view.inert = true", detail3)
        self.assertIn("history.pushState({ phk: 'coursemodal' }", detail3)
        self.assertIn("function closeModal({ viaPopstate = false } = {})", HTML)
        closem = function_body("closeModal")
        self.assertIn("view.inert = false", closem)
        self.assertIn("modalStackedOnAccount = false", closem)
        self.assertIn("if (accountViewOpen) {", closem)
        # 叠层详情的 Escape 与焦点优先于个人视图
        self.assertLess(
            keydown_new.index("modalStackedOnAccount"),
            keydown_new.index("if (accountViewOpen) {"),
        )
        # popstate 按目标历史状态决策：先关叠层详情，再关个人视图
        pop = HTML[HTML.index("window.addEventListener('popstate'"):][:1000]
        self.assertIn("const phk = e.state && e.state.phk;", pop)
        self.assertIn("phk !== 'coursemodal'", pop)
        self.assertIn("phk !== 'account'", pop)
        self.assertIn("closeModal({ viaPopstate: true })", pop)

    def test_favorite_key_matches_server_normalization(self):
        term_map = re.search(r"const TERM_BY_PREFIX = \{[^}]*\};", HTML).group(0)
        level_map = re.search(r"const LEVEL_BY_PREFIX = \{[^}]*\};", HTML).group(0)
        self.run_node(
            f"""
            const assert = require('node:assert/strict');
            {term_map}
            {level_map}
            {function_source("favoriteKey")}
            assert.equal(
              favoriteKey({{ id: 'a1', course_code: '04831180', class_no: 1, teacher: ' 张三(教授) ' }}),
              'fall|ug|04831180|1|张三(教授)'
            );
            assert.equal(favoriteKey({{ id: 'r12', course_code: 'X', class_no: '2', teacher: '' }}), 'fall|gr|X|2|');
            assert.equal(favoriteKey({{ id: 'u5', course_code: 'C', class_no: null, teacher: null }}), 'spring|ug|C||');
            assert.equal(favoriteKey({{ id: 'g5', course_code: 'C', class_no: '1', teacher: 't' }}), 'spring|gr|C|1|t');
            assert.equal(favoriteKey({{ id: 's5', course_code: 'C', class_no: '1', teacher: 't' }}), 'summer|ug|C|1|t');
            assert.equal(favoriteKey({{ id: 'a7', course_code: 'C', class_no: 1, teacher: 't' }}),
                         favoriteKey({{ id: 'a9', course_code: 'C', class_no: '1', teacher: 't ' }}));
            """
        )


if __name__ == "__main__":
    unittest.main()
