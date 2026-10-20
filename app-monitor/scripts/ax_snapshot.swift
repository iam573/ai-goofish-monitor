import AppKit
import ApplicationServices
import Foundation

var lastInteractionMethod = "AXPress"
var lastTextEntryMethod = ""
var lastSearchFieldRole = ""
var lastSearchFieldValue = ""
var lastTextEntryError = ""
var activeFilterCategoryLabel: String?

func attribute(_ element: AXUIElement, _ name: CFString) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name, &value) == .success else { return nil }
    return value as AnyObject?
}

func textAttribute(_ element: AXUIElement, _ name: CFString) -> String? {
    guard let value = attribute(element, name) else { return nil }
    if let string = value as? String { return string }
    if let number = value as? NSNumber { return number.stringValue }
    return nil
}

func pointAttribute(_ element: AXUIElement, _ name: CFString) -> [String: Double]? {
    guard let value = attribute(element, name), CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    let axValue = value as! AXValue
    if AXValueGetType(axValue) == .cgPoint {
        var point = CGPoint.zero
        guard AXValueGetValue(axValue, .cgPoint, &point) else { return nil }
        return ["x": point.x, "y": point.y]
    }
    if AXValueGetType(axValue) == .cgSize {
        var size = CGSize.zero
        guard AXValueGetValue(axValue, .cgSize, &size) else { return nil }
        return ["width": size.width, "height": size.height]
    }
    return nil
}

func label(_ element: AXUIElement) -> String {
    for name in [kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute, kAXHelpAttribute] {
        if let text = textAttribute(element, name as CFString), !text.isEmpty { return text }
    }
    return ""
}

func collect(_ element: AXUIElement, depth: Int, nodes: inout [[String: Any]]) {
    guard depth <= 18, nodes.count < 5000 else { return }
    var node: [String: Any] = ["depth": depth]
    if let role = textAttribute(element, kAXRoleAttribute as CFString) { node["role"] = role }
    if let title = textAttribute(element, kAXTitleAttribute as CFString) { node["title"] = title }
    if let value = textAttribute(element, kAXValueAttribute as CFString) { node["value"] = value }
    if let desc = textAttribute(element, kAXDescriptionAttribute as CFString) { node["description"] = desc }
    if let identifier = textAttribute(element, kAXIdentifierAttribute as CFString) { node["identifier"] = identifier }
    if let position = pointAttribute(element, kAXPositionAttribute as CFString) { node["position"] = position }
    if let size = pointAttribute(element, kAXSizeAttribute as CFString) { node["size"] = size }
    if node.count > 2 { nodes.append(node) }

    guard let children = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] else { return }
    for child in children { collect(child, depth: depth + 1, nodes: &nodes) }
}

func frame(_ element: AXUIElement) -> CGRect? {
    guard let position = attribute(element, kAXPositionAttribute as CFString),
          let size = attribute(element, kAXSizeAttribute as CFString),
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var extent = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
          AXValueGetValue(size as! AXValue, .cgSize, &extent) else { return nil }
    return CGRect(origin: point, size: extent)
}

func parent(_ element: AXUIElement) -> AXUIElement? {
    guard let value = attribute(element, kAXParentAttribute as CFString),
          CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}

/// 广度优先列出所有带文字的元素。
func labeled(_ roots: [AXUIElement]) -> [(AXUIElement, String)] {
    var queue: [(AXUIElement, Int)] = roots.map { ($0, 0) }
    var result: [(AXUIElement, String)] = []
    var visited = 0
    while !queue.isEmpty && visited < 8000 {
        let (element, depth) = queue.removeFirst()
        visited += 1
        let text = label(element).trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { result.append((element, text)) }
        if depth < 18, let children = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] {
            queue.append(contentsOf: children.map { ($0, depth + 1) })
        }
    }
    return result
}

/// 元素的「文字@位置」，用来判断点击后哪些元素是新出现的（例如弹出的筛选面板）。
func signature(_ element: AXUIElement, _ text: String) -> String {
    guard let rect = frame(element) else { return text }
    return "\(text)@\(Int(rect.minX)),\(Int(rect.minY))"
}

func signatures(_ roots: [AXUIElement]) -> Set<String> {
    Set(labeled(roots).map { signature($0.0, $0.1) })
}

/// 查找候选元素并排序：上一步点击后新出现的优先，其次文字完全相同，再次文字最短。
/// 模糊匹配：包含目标文字且长度相近，或以目标文字开头（例如「查看 2.3万 件」）。
func candidates(_ target: String, in roots: [AXUIElement], before: Set<String>?) -> [AXUIElement] {
    var scored: [(AXUIElement, Int, Int, Int, Int)] = []
    for (element, text) in labeled(roots) {
        let exact = text == target
        let close = text.contains(target) && text.count <= target.count + 3
        let prefixed = text.hasPrefix(target) && text.count <= target.count + 12
        guard exact || close || prefixed else { continue }
        let fresh = before.map { !$0.contains(signature(element, text)) } ?? true
        let role = textAttribute(element, kAXRoleAttribute as CFString) ?? ""
        let isButton = role == (kAXButtonRole as String)
        scored.append((element, fresh ? 0 : 1, exact ? 0 : 1, isButton ? 0 : 1, text.count))
    }
    scored.sort { ($0.1, $0.2, $0.3, $0.4) < ($1.1, $1.2, $1.3, $1.4) }
    return scored.map { $0.0 }
}

func compactCapacity(_ value: String) -> String {
    value.uppercased()
        .replacingOccurrences(of: "GB", with: "G")
        .replacingOccurrences(of: "TB", with: "T")
        .filter { $0.isNumber || $0 == "G" || $0 == "T" }
}

func isCapacityLabel(_ value: String) -> Bool {
    compactCapacity(value).range(of: #"^\d+[GT]$"#, options: .regularExpression) != nil
}

/// Capacity labels also appear on product cards. Only click an option inside the open capacity
/// picker, bounded by its category heading or its 「重置」/「确定」 footer controls.
func filterCapacityCandidate(
    _ target: String,
    in roots: [AXUIElement],
    categoryLabel: String = "存储容量"
) -> AXUIElement? {
    guard isCapacityLabel(target) else { return nil }
    let elements = labeled(roots).compactMap { element, text -> (AXUIElement, String, CGRect)? in
        guard let rect = frame(element), rect.width > 0, rect.height > 0 else { return nil }
        return (element, text, rect)
    }
    let headings = elements.filter {
        if categoryLabel == "存储容量" { return $0.1 == categoryLabel }
        return isCapacityLabel($0.1) && compactCapacity($0.1) == compactCapacity(categoryLabel)
    }.map { $0.2 }
    let confirms = elements.filter {
        $0.1 == "确定" && role($0.0) == (kAXButtonRole as String)
    }
    let resets = elements.filter {
        $0.1 == "重置" && role($0.0) == (kAXButtonRole as String)
    }
    guard let sort = sortBarCandidate("新发", in: roots), let sortRect = frame(sort) else {
        return nil
    }
    // On the current macOS App, opening 「存储容量」 replaces the category row with a grid.
    // The heading disappears, and the grid is bounded by the reset/confirm buttons instead.
    // Match only within that bounded panel area; never search the full results page.
    if let confirm = confirms.first?.2, let reset = resets.first?.2,
       abs(confirm.midY - reset.midY) <= 24 {
        let matches = elements.filter { _, text, rect in
            return compactCapacity(text) == compactCapacity(target)
                // AXStaticText options in this grid report the width of the whole column
                // (about 278pt), not the rendered text width.
                && rect.width <= 400
                && rect.height <= 80
                && rect.minY > sortRect.maxY + 35
                && rect.maxY < min(confirm.minY, reset.minY)
                && rect.minX >= reset.minX - 24
                && rect.maxX <= confirm.maxX + 24
        }
        if let match = matches.min(by: { $0.2.midY < $1.2.midY }) {
            return match.0
        }
    }
    let confirmFrames = confirms.map { $0.2 }
    for heading in headings {
        guard let confirm = confirmFrames
            .filter({ $0.minY > heading.midY })
            .min(by: { abs($0.midX - heading.midX) < abs($1.midX - heading.midX) }) else { continue }
        let matches = elements.filter { _, text, rect in
            return compactCapacity(text) == compactCapacity(target)
                && rect.width <= 400
                && rect.height <= 80
                && rect.minY > sortRect.maxY + 35
                && rect.minY >= heading.minY - 12
                && rect.maxY <= confirm.minY + 8
                && rect.midX >= heading.minX - 40
                && rect.midX <= confirm.maxX + 20
        }
        if let match = matches.min(by: { abs($0.2.midY - heading.midY) < abs($1.2.midY - heading.midY) }) {
            return match.0
        }
    }
    return nil
}

func visibleFilterCapacityLabels(in roots: [AXUIElement]) -> [String] {
    let elements = labeled(roots).compactMap { element, text -> (String, CGRect)? in
        guard isCapacityLabel(text), let rect = frame(element), rect.width > 0, rect.height > 0 else { return nil }
        return (text, rect)
    }
    let confirms = labeled(roots).compactMap { element, text -> CGRect? in
        guard text == "确定", role(element) == (kAXButtonRole as String) else { return nil }
        return frame(element)
    }
    let resets = labeled(roots).compactMap { element, text -> CGRect? in
        guard text == "重置", role(element) == (kAXButtonRole as String) else { return nil }
        return frame(element)
    }
    guard let confirm = confirms.first, let reset = resets.first,
          abs(confirm.midY - reset.midY) <= 24 else { return [] }
    return Array(Set(elements.compactMap { text, rect in
        rect.maxY < min(confirm.minY, reset.minY)
            && rect.minX >= reset.minX - 24
            && rect.maxX <= confirm.maxX + 24
            ? text : nil
    })).sorted()
}

/// The selected capacity replaces 「存储容量」 in the quick-filter row. Restrict the category
/// lookup to the row immediately below the sort bar, away from same-valued product cards.
func filterCategoryCandidate(
    _ target: String,
    in roots: [AXUIElement]
) -> AXUIElement? {
    guard let filter = sortBarCandidate("筛选", in: roots), let sortRect = frame(filter) else {
        return nil
    }
    let matches = labeled(roots).compactMap { element, text -> (AXUIElement, CGRect)? in
        let sameLabel = target == "存储容量"
            ? text == target
            : isCapacityLabel(text) && compactCapacity(text) == compactCapacity(target)
        guard sameLabel, let rect = frame(element), rect.width > 0, rect.height > 0,
              rect.minY >= sortRect.minY + 15,
              rect.minY <= sortRect.minY + 65,
              rect.minX >= sortRect.minX - 400,
              rect.maxX < sortRect.minX else { return nil }
        return (element, rect)
    }
    return matches.min { $0.1.minY < $1.1.minY }?.0
}

/// Filter options/categories must respond directly to AXPress. Do not climb to a card parent:
/// that fallback can open a product detail page when the text belongs to a listing card.
func pressFilterElementOnly(_ element: AXUIElement) -> Bool {
    guard AXUIElementPerformAction(element, kAXPressAction as CFString) == .success else { return false }
    lastInteractionMethod = "AXPress(筛选元素自身)"
    return true
}

/// This App reports AXPress success for a capacity option without selecting it. The option
/// has already been constrained to the reset/confirm-bounded grid, so click its center while
/// the App is foreground. Never use this click path for an unbounded product-card label.
func clickCapacityOption(_ element: AXUIElement, app: NSRunningApplication) -> Bool {
    if NSWorkspace.shared.frontmostApplication?.processIdentifier != app.processIdentifier {
        if app.activate(options: []), waitUntil(1.5, test: {
            NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
        }) == false {
            guard let bundleURL = app.bundleURL else { return false }
            let opener = Process()
            opener.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            opener.arguments = ["-a", bundleURL.path]
            guard (try? opener.run()) != nil else { return false }
            opener.waitUntilExit()
            guard opener.terminationStatus == 0 else { return false }
        }
    }
    guard waitUntil(4, test: {
        NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
    }), let rect = frame(element), rect.width > 0, rect.height > 0 else { return false }
    let point = CGPoint(x: rect.midX, y: rect.midY)
    guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                             mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                           mouseCursorPosition: point, mouseButton: .left) else { return false }
    down.post(tap: .cghidEventTap)
    usleep(60_000)
    up.post(tap: .cghidEventTap)
    lastInteractionMethod = "CGEvent(容量选项，前台，\(Int(point.x)),\(Int(point.y)))"
    return true
}

/// Sort and region controls are AXStaticText and can have same-label matches elsewhere on the
/// page. Restrict them to the single row bracketed by 「综合」 and 「筛选」.
func sortBarCandidate(_ target: String, in roots: [AXUIElement]) -> AXUIElement? {
    let controls = ["综合", "价格", "降价", "新发", "筛选"]
    let labeledElements = labeled(roots).compactMap { element, text -> (AXUIElement, String, CGRect)? in
        guard (controls.contains(text) || text == target),
              let rect = frame(element), rect.width > 0, rect.height > 0 else { return nil }
        return (element, text, rect)
    }
    let latest = labeledElements.filter { $0.1 == "新发" }.map { $0.2.minY }
    let filters = labeledElements.filter { $0.1 == "筛选" }.map { $0.2.minY }
    let comprehensives = labeledElements.filter { $0.1 == "综合" }.map { $0.2.minY }
    for y in latest {
        guard let comprehensiveY = comprehensives.min(by: { abs($0 - y) < abs($1 - y) }),
              let filterY = filters.min(by: { abs($0 - y) < abs($1 - y) }),
              abs(comprehensiveY - y) <= 8, abs(filterY - y) <= 8 else { continue }
        let row = labeledElements.filter { abs($0.2.midY - y) <= 8 }
        guard let left = row.filter({ $0.1 == "综合" }).map({ $0.2.minX }).min(),
              let right = row.filter({ $0.1 == "筛选" }).map({ $0.2.maxX }).max(), left < right else { continue }
        if let match = row.first(where: { $0.1 == target && $0.2.midX >= left && $0.2.midX <= right }) {
            return match.0
        }
    }
    return nil
}

/// 只通过辅助功能动作触发元素或其紧邻的可点击父元素，不主动移动鼠标或调用应用激活。
func pressAccessibilityOnly(_ element: AXUIElement) -> Bool {
    if AXUIElementPerformAction(element, kAXPressAction as CFString) == .success {
        lastInteractionMethod = "AXPress"
        return true
    }
    let childRect = frame(element)
    var current: AXUIElement? = parent(element)
    for _ in 0..<4 {
        guard let node = current, let parentRect = frame(node), let childRect,
              parentRect.width > 0, parentRect.height > 0,
              parentRect.width <= childRect.width + 120,
              parentRect.height <= childRect.height + 80,
              AXUIElementPerformAction(node, kAXPressAction as CFString) == .success else {
            current = current.flatMap(parent)
            continue
        }
        lastInteractionMethod = "AXPress(相邻父元素)"
        return true
    }
    return false
}

/// Experimental: send a mouse click directly to the Goofish process, bypassing the global event stream.
func postMouseClickToPid(_ element: AXUIElement, app: NSRunningApplication) -> Bool {
    guard let rect = frame(element), rect.width > 0, rect.height > 0 else { return false }
    let point = CGPoint(x: rect.midX, y: rect.midY)
    guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) else { return false }
    down.postToPid(app.processIdentifier)
    usleep(60_000)
    up.postToPid(app.processIdentifier)
    lastInteractionMethod = "CGEvent.postToPid"
    return true
}

func roots(of app: NSRunningApplication) -> [AXUIElement] {
    let root = AXUIElementCreateApplication(app.processIdentifier)
    let windows = (attribute(root, kAXWindowsAttribute as CFString) as? [AXUIElement]) ?? []
    return windows.isEmpty ? [root] : windows
}

func role(_ element: AXUIElement) -> String {
    textAttribute(element, kAXRoleAttribute as CFString) ?? ""
}

func allElements(_ roots: [AXUIElement]) -> [AXUIElement] {
    var queue: [(AXUIElement, Int)] = roots.map { ($0, 0) }
    var result: [AXUIElement] = []
    var visited = 0
    while !queue.isEmpty && visited < 8000 {
        let (element, depth) = queue.removeFirst()
        visited += 1
        result.append(element)
        if depth < 18, let children = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] {
            queue.append(contentsOf: children.map { ($0, depth + 1) })
        }
    }
    return result
}

func waitUntil(_ timeout: TimeInterval, test: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
        if test() { return true }
        usleep(300_000)
    } while Date() < deadline
    return test()
}

func normalizedCity(_ value: String) -> String {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
    return text.hasSuffix("市") ? String(text.dropLast()) : text
}

func bottomTabs(app: NSRunningApplication) -> [(AXUIElement, String)] {
    guard let window = roots(of: app).compactMap({ frame($0) }).first(where: { $0.height > 300 }) else {
        return []
    }
    return labeled(roots(of: app)).filter { element, text in
        guard role(element) == (kAXButtonRole as String), let rect = frame(element) else { return false }
        return rect.midY > window.minY + window.height * 0.82 && rect.width > 50 && !text.isEmpty
    }.sorted { (frame($0.0)?.minX ?? 0) < (frame($1.0)?.minX ?? 0) }
}

func clickBottomRegion(_ city: String, app: NSRunningApplication) -> Bool {
    let wanted = normalizedCity(city)
    let tabs = bottomTabs(app: app)
    guard let tab = tabs.first(where: { normalizedCity($0.1) == wanted }) else { return false }
    return pressAccessibilityOnly(tab.0)
}

func topWideElement(app: NSRunningApplication, wantedRole: String) -> AXUIElement? {
    guard let window = roots(of: app).compactMap({ frame($0) }).first(where: { $0.height > 300 }) else {
        return nil
    }
    return allElements(roots(of: app)).first { element in
        guard role(element) == wantedRole, let rect = frame(element) else { return false }
        return rect.minY >= window.minY && rect.minY <= window.minY + 80
            && rect.width >= window.width * 0.55 && rect.height >= 10 && rect.height <= 60
    }
}

func keyPress(_ code: CGKeyCode, flags: CGEventFlags = []) {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else { return }
    down.flags = flags
    up.flags = flags
    down.post(tap: .cghidEventTap)
    usleep(40_000)
    up.post(tap: .cghidEventTap)
}

func setSearchText(_ text: String, app: NSRunningApplication) -> Bool {
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    guard let focusedValue = attribute(appElement, kAXFocusedUIElementAttribute as CFString) else {
        lastTextEntryError = "没有可访问的焦点控件"
        return false
    }
    let focused = focusedValue as! AXUIElement
    let fieldRole = textAttribute(focused, kAXRoleAttribute as CFString) ?? ""
    lastSearchFieldRole = fieldRole
    var settable = DarwinBoolean(false)
    let valueIsSettable = AXUIElementIsAttributeSettable(
        focused, kAXValueAttribute as CFString, &settable
    ) == .success && settable.boolValue
    // The App's actual query editor is an AXTextArea with a zero-sized frame whose origin
    // sits inside the wide search button. Require both that position and writable AXValue,
    // so a stale focus elsewhere cannot receive the search keyword.
    let isSearchTextArea: Bool = {
        guard fieldRole == "AXTextArea", valueIsSettable,
              let focusedFrame = frame(focused),
              let searchButton = topWideElement(app: app, wantedRole: kAXButtonRole as String),
              let searchFrame = frame(searchButton) else { return false }
        return searchFrame.insetBy(dx: -12, dy: -10).contains(focusedFrame.origin)
    }()
    guard [kAXTextFieldRole as String, kAXComboBoxRole as String].contains(fieldRole)
        || isSearchTextArea else {
        lastTextEntryError = "焦点控件不是文本输入框（role=\(fieldRole)）"
        return false
    }
    var valueWriteWasIgnored = false
    if valueIsSettable,
       AXUIElementSetAttributeValue(focused, kAXValueAttribute as CFString, text as CFTypeRef) == .success {
        usleep(100_000)
        let actual = (textAttribute(focused, kAXValueAttribute as CFString) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        lastSearchFieldValue = actual
        if actual == text {
            lastTextEntryMethod = "AXValue（已回读校验）"
            return true
        }
        valueWriteWasIgnored = true
    }
    // Never send global keyboard events to an unrelated foreground app.
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else {
        lastTextEntryError = valueWriteWasIgnored
            ? "AXValue 写入后回读未得到目标词，后台定向键盘事件也未生效；闲鱼不在前台"
            : "AXValue 不可写，且闲鱼不是前台应用"
        return false
    }
    replaceFocusedText(text)
    usleep(100_000)
    let actual = (textAttribute(focused, kAXValueAttribute as CFString) ?? "")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    lastSearchFieldValue = actual
    guard actual == text else {
        lastTextEntryError = "键盘输入后回读不一致：期望「\(text)」，实际「\(actual)」"
        return false
    }
    lastTextEntryMethod = "CGEvent 键盘（闲鱼已在前台）"
    return true
}

func replaceFocusedText(_ text: String) {
    keyPress(0, flags: .maskCommand) // Command-A
    var units = Array(text.utf16)
    if let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
       let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) {
        down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}

func returnToHome(app: NSRunningApplication) -> Bool {
    for _ in 0..<5 {
        if bottomTabs(app: app).count >= 2 { return true }
        guard clickStep("返回", app: app) else { return false }
        usleep(800_000)
    }
    return bottomTabs(app: app).count >= 2
}

/// 从首页第二个地区 Tab 发起搜索。这样结果页会继承该地区，无需再打开「区域」面板。
func searchFromHomeRegion(city: String, keyword: String, app: NSRunningApplication) -> (Bool, String) {
    guard returnToHome(app: app) else { return (false, "无法返回闲鱼首页") }
    let tabs = bottomTabs(app: app)
    guard tabs.count >= 2 else { return (false, "首页底部 Tab 不完整") }
    guard let matchingTab = tabs.first(where: { normalizedCity($0.1) == normalizedCity(city) }) else {
        return (false, "首页当前可访问的地区 Tab 中没有「\(city)」")
    }
    guard clickBottomRegion(city, app: app) else { return (false, "无法点击首页地区 Tab「\(city)」") }
    usleep(1_000_000)

    guard let homeSearch = topWideElement(app: app, wantedRole: "AXGenericElement"),
          pressAccessibilityOnly(homeSearch) else {
        return (false, "地区首页搜索框不支持 AXPress，未触发鼠标回退")
    }
    guard waitUntil(6, test: {
        topWideElement(app: app, wantedRole: kAXButtonRole as String) != nil
            && labeled(roots(of: app)).contains(where: { $0.1 == "搜索" })
    }) else { return (false, "搜索输入页加载超时") }

    guard let input = topWideElement(app: app, wantedRole: kAXButtonRole as String),
          pressAccessibilityOnly(input) else {
        return (false, "搜索输入框不支持 AXPress，未触发鼠标回退")
    }
    usleep(200_000)
    guard setSearchText(keyword, app: app) else {
        return (false, "搜索词输入校验失败：\(lastTextEntryError)；为避免误搜，已跳过提交")
    }
    usleep(300_000)
    guard lastSearchFieldValue == keyword else {
        return (false, "提交前搜索词校验失败：期望「\(keyword)」，实际「\(lastSearchFieldValue)」")
    }
    guard clickStep("搜索", app: app) else { return (false, "输入关键词后找不到搜索按钮") }
    guard waitUntil(15, test: {
        let texts = Set(labeled(roots(of: app)).map { $0.1 })
        return texts.contains("新发") && texts.contains("降价")
    }) else { return (false, "搜索结果排序栏加载超时") }
    return (true, matchingTab.1)
}

/// 点击一个步骤；界面可能还在加载，最多重试 6 次（约 3 秒）。
func clickStep(_ target: String, app: NSRunningApplication, before: Set<String>? = nil) -> Bool {
    for attempt in 0..<6 {
        if attempt > 0 { usleep(500_000) }
        let currentRoots = roots(of: app)
        if let sortElement = sortBarCandidate(target, in: currentRoots) {
            // These controls are exposed as text nodes; AXPress avoids moving the cursor.
            if pressAccessibilityOnly(sortElement) { return true }
            continue
        }
        if isCapacityLabel(target) {
            let category = activeFilterCategoryLabel ?? "存储容量"
            guard let option = filterCapacityCandidate(
                target, in: currentRoots, categoryLabel: category
            ) else { continue }
            if clickCapacityOption(option, app: app) { return true }
            continue
        }
        let element = candidates(target, in: currentRoots, before: before).first
        if let element, pressAccessibilityOnly(element) { return true }
    }
    return false
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    FileHandle.standardError.write(Data("usage: ax_snapshot <bundle-id> [--click text | --click-path a|b|?c | --home-search city keyword | --scroll screens | --escape]\n".utf8))
    exit(2)
}
let bundleID = arguments[1]
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
    FileHandle.standardError.write(Data("闲鱼 App 未运行\n".utf8))
    exit(3)
}
let traversalRoots = roots(of: app)

if arguments.count >= 5 && arguments[2] == "--home-search" {
    let (success, message) = searchFromHomeRegion(city: arguments[3], keyword: arguments[4], app: app)
    if !success {
        FileHandle.standardError.write(Data("首页地区搜索失败：\(message)\n".utf8))
        exit(6)
    }
    let payload = try JSONSerialization.data(
        withJSONObject: [
            "searched": true, "region": message, "keyword": arguments[4],
            "method": lastInteractionMethod, "text_method": lastTextEntryMethod,
            "field_role": lastSearchFieldRole, "field_value": lastSearchFieldValue,
        ], options: []
    )
    FileHandle.standardOutput.write(payload)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

if arguments.count >= 4 && arguments[2] == "--click" {
    let target = arguments[3]
    if !clickStep(target, app: app) {
        FileHandle.standardError.write(Data("找不到可点击元素：\(target)\n".utf8))
        exit(4)
    }
    let payload = try JSONSerialization.data(withJSONObject: ["clicked": true, "method": lastInteractionMethod], options: [])
    FileHandle.standardOutput.write(payload)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

if arguments.count >= 4 && arguments[2] == "--click-pid" {
    let target = arguments[3]
    let matches = labeled(traversalRoots).filter { $0.1 == target && role($0.0) == (kAXButtonRole as String) }
    let pointerBefore = NSEvent.mouseLocation
    let frontmostBefore = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "none"
    guard let element = matches.first?.0, postMouseClickToPid(element, app: app) else {
        FileHandle.standardError.write(Data("找不到可定向点击的按钮：\(target)\n".utf8))
        exit(8)
    }
    usleep(300_000)
    let pointerAfter = NSEvent.mouseLocation
    let frontmostAfter = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "none"
    let payload = try JSONSerialization.data(withJSONObject: [
        "clicked": true, "target": target, "method": lastInteractionMethod,
        "pointer_before": ["x": pointerBefore.x, "y": pointerBefore.y],
        "pointer_after": ["x": pointerAfter.x, "y": pointerAfter.y],
        "frontmost_before": frontmostBefore, "frontmost_after": frontmostAfter,
        "goofish_active": app.isActive,
    ], options: [])
    FileHandle.standardOutput.write(payload)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

// --escape：按一次 Esc，关闭点击路径失败后留在页面上的筛选 / 区域面板。
if arguments.count >= 3 && arguments[2] == "--escape" {
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    let focused = attribute(appElement, kAXFocusedUIElementAttribute as CFString).map { $0 as! AXUIElement }
    let cancelAction = "AXCancel" as CFString
    if let focused, AXUIElementPerformAction(focused, cancelAction) == .success {
        print("{\"escaped\":true,\"method\":\"AXCancel\"}")
        exit(0)
    }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else {
        FileHandle.standardError.write(Data("当前窗口不在前台，且筛选面板不支持 AXCancel；未切换焦点或发送按键\n".utf8))
        exit(7)
    }
    if let down = CGEvent(keyboardEventSource: nil, virtualKey: 53, keyDown: true),
       let up = CGEvent(keyboardEventSource: nil, virtualKey: 53, keyDown: false) {
        down.post(tap: .cghidEventTap)
        usleep(30_000)
        up.post(tap: .cghidEventTap)
    }
    print("{\"escaped\":true,\"method\":\"CGEvent 键盘（闲鱼已在前台）\"}")
    exit(0)
}

// --scroll [屏数]：把鼠标移到窗口中部并向下滚动，用于加载同一页更下方的商品。
if arguments.count >= 3 && arguments[2] == "--scroll" {
    let screens = arguments.count >= 4 ? (Double(arguments[3]) ?? 1) : 1
    guard let rect = traversalRoots.compactMap({ frame($0) }).first(where: { $0.height > 200 }) else {
        FileHandle.standardError.write(Data("找不到闲鱼窗口\n".utf8))
        exit(5)
    }
    let point = CGPoint(x: rect.midX, y: rect.minY + rect.height * 0.7)
    let steps = max(1, Int(rect.height * 0.75 * abs(screens) / 60))
    let wheelDelta: Int32 = screens >= 0 ? -60 : 60
    for _ in 0..<steps {
        if let wheel = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1, wheel1: wheelDelta, wheel2: 0, wheel3: 0) {
            wheel.location = point
            wheel.post(tap: .cghidEventTap)
        }
        usleep(25_000)
    }
    print("{\"scrolled\":\(steps)}")
    exit(0)
}

// --click-path "区域|福建|厦门|?确定"：按顺序点击，"?" 开头的步骤找不到时跳过。
if arguments.count >= 4 && arguments[2] == "--click-path" {
    var clicked: [String] = []
    var skipped: [String] = []
    var actions: [[String: String]] = []
    var before: Set<String>? = nil
    let steps = arguments[3].split(separator: "|").map({ String($0).trimmingCharacters(in: .whitespaces) }).filter { !$0.isEmpty }
    for (stepIndex, raw) in steps.enumerated() {
        let optional = raw.hasPrefix("?")
        let target = optional ? String(raw.dropFirst()) : raw
        let currentRoots = roots(of: app)
        let current = signatures(currentRoots)
        let remainingSteps = steps.dropFirst(stepIndex + 1)
        let selectsCapacityCategory = (stepIndex == 0 || clicked.last == "筛选")
            && (target == "存储容量" || isCapacityLabel(target))
            && remainingSteps.contains(where: isCapacityLabel)
        if selectsCapacityCategory {
            guard let category = filterCategoryCandidate(target, in: currentRoots),
                  pressFilterElementOnly(category) else {
                FileHandle.standardError.write(Data("找不到当前容量分类入口：\(target)\n".utf8))
                exit(4)
            }
            activeFilterCategoryLabel = target
            clicked.append(target)
            actions.append(["target": target, "method": "AXPress(容量分类文本)"])
            before = current
            usleep(800_000)
            continue
        }
        let isCapacityOption = target.range(of: #"^\d+(?:GB?|TB?)$"#, options: .regularExpression) != nil
        if isCapacityOption && remainingSteps.contains("确定") {
            let hasConfirm = labeled(currentRoots).contains { $0.1 == "确定" }
            guard hasConfirm else {
                FileHandle.standardError.write(Data("筛选面板未显示「确定」，为避免误点商品卡片，未点击容量项：\(target)\n".utf8))
                exit(4)
            }
        }
        if clickStep(target, app: app, before: before) {
            clicked.append(target)
            actions.append(["target": target, "method": lastInteractionMethod])
            before = current
            usleep(800_000)
        } else if optional {
            skipped.append(target)
        } else {
            let capacityHint = isCapacityOption
                ? "；筛选面板可见容量：\(visibleFilterCapacityLabels(in: roots(of: app)).joined(separator: ", "))"
                : ""
            FileHandle.standardError.write(Data("找不到可点击元素：\(target)\(capacityHint)（已点击：\(clicked.joined(separator: " > "))）\n".utf8))
            exit(4)
        }
    }
    let payload = try JSONSerialization.data(withJSONObject: ["clicked": clicked, "skipped": skipped, "actions": actions], options: [])
    FileHandle.standardOutput.write(payload)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

var nodes: [[String: Any]] = []
for traversalRoot in traversalRoots {
    collect(traversalRoot, depth: 0, nodes: &nodes)
}
let data = try JSONSerialization.data(withJSONObject: nodes, options: [])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
