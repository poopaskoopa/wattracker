import Foundation

/// A Gregorian month used by the calendar screen.
///
/// Calendar objects carry rider-local, date-only values. Keeping the month as
/// year/month components avoids converting a date-only value through the
/// phone's timezone (which can move it across midnight), while the UTC
/// calendar makes month arithmetic deterministic.
struct CalendarMonth: Hashable, Sendable, Equatable {
    let year: Int
    let month: Int

    init(year: Int, month: Int) {
        precondition((1...12).contains(month))
        self.year = year
        self.month = month
    }

    static func current(date: Date = Date()) -> CalendarMonth {
        var calendar = Self.gregorian
        calendar.timeZone = .current
        let components = calendar.dateComponents([.year, .month], from: date)
        return CalendarMonth(
            year: components.year ?? 2000,
            month: components.month ?? 1
        )
    }

    var id: String { String(format: "%04d-%02d", year, month) }

    var firstDate: Date {
        Self.gregorian.date(from: DateComponents(year: year, month: month, day: 1))!
    }

    var dayCount: Int {
        Self.gregorian.range(of: .day, in: .month, for: firstDate)?.count ?? 28
    }

    /// Number of empty cells before the first day when weeks begin on Monday.
    var leadingEmptyDays: Int {
        // Foundation's weekday numbering is Sunday = 1 ... Saturday = 7.
        (Self.gregorian.component(.weekday, from: firstDate) + 5) % 7
    }

    var title: String {
        let formatter = DateFormatter()
        formatter.locale = .current
        formatter.calendar = Self.gregorian
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.setLocalizedDateFormatFromTemplate("LLLLyyyy")
        return formatter.string(from: firstDate)
    }

    func adding(months: Int) -> CalendarMonth {
        let date = Self.gregorian.date(byAdding: .month, value: months, to: firstDate)!
        let components = Self.gregorian.dateComponents([.year, .month], from: date)
        return CalendarMonth(year: components.year!, month: components.month!)
    }

    func isoDate(day: Int) -> String {
        precondition((1...dayCount).contains(day))
        return String(format: "%04d-%02d-%02d", year, month, day)
    }

    static func fromISODate(_ value: String) -> CalendarMonth? {
        guard value.count == 10,
              value[value.index(value.startIndex, offsetBy: 4)] == "-",
              value[value.index(value.startIndex, offsetBy: 7)] == "-",
              let date = ISODate.parse(value)
        else { return nil }
        let components = Self.gregorian.dateComponents([.year, .month], from: date)
        guard let year = components.year, let month = components.month else { return nil }
        return CalendarMonth(year: year, month: month)
    }

    fileprivate static let gregorian: Foundation.Calendar = {
        var calendar = Foundation.Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        calendar.locale = Locale(identifier: "en_US_POSIX")
        calendar.firstWeekday = 2
        return calendar
    }()
}

struct CalendarWorkoutProfileBlock: Identifiable, Sendable, Equatable {
    let start: Double
    let end: Double
    let durationS: Double
    let targetStart: Double?
    let targetEnd: Double?
    let kind: String
    let label: String?
    let text: String?
    let free: Bool
    let segmentIndex: Int?

    init(
        start: Double,
        end: Double,
        durationS: Double,
        targetStart: Double?,
        targetEnd: Double?,
        kind: String,
        label: String?,
        text: String?,
        free: Bool,
        segmentIndex: Int? = nil
    ) {
        self.start = start
        self.end = end
        self.durationS = durationS
        self.targetStart = targetStart
        self.targetEnd = targetEnd
        self.kind = kind
        self.label = label
        self.text = text
        self.free = free
        self.segmentIndex = segmentIndex
    }

    var id: String { "\(start)-\(end)-\(kind)" }
}

enum CalendarWorkoutFormatting {
    static func stepDescription(
        _ step: CalendarWorkoutProfileBlock,
        ftp: Double?
    ) -> String {
        let duration = duration(step.durationS)
        if step.free {
            return "\(duration) max effort — no target"
        }
        let start = target(step.targetStart, ftp: ftp)
        let end = target(step.targetEnd, ftp: ftp)
        let percentStart = percent(step.targetStart)
        let percentEnd = percent(step.targetEnd)
        guard let start, let end else {
            return "\(duration) at \(percentStart ?? percentEnd ?? "unknown") FTP"
        }
        if start == end {
            if let percentStart {
                return "\(duration) at \(start) W (\(percentStart) FTP)"
            }
            return "\(duration) at \(start) W"
        }
        if let percentStart, let percentEnd {
            return "\(duration) at \(start)–\(end) W (\(percentStart)–\(percentEnd) FTP)"
        }
        return "\(duration) at \(start)–\(end) W"
    }

    static func stepDescriptions(
        _ steps: [CalendarWorkoutProfileBlock],
        ftp: Double?
    ) -> [String] {
        var result: [String] = []
        var index = 0
        while index < steps.count {
            let step = steps[index]
            guard step.kind == "intervals",
                  let label = step.label, !label.isEmpty else {
                result.append(stepDescription(step, ftp: ftp))
                index += 1
                continue
            }

            let segmentIndex = step.segmentIndex
            var group = [step]
            var next = index + 1
            while next < steps.count,
                  steps[next].kind == "intervals",
                  steps[next].label == label {
                if let segmentIndex,
                   let nextSegmentIndex = steps[next].segmentIndex,
                   nextSegmentIndex != segmentIndex {
                    break
                }
                group.append(steps[next])
                next += 1
            }
            result.append(intervalDescription(label, group, ftp: ftp))
            index = next
        }
        return result
    }

    static func coachingTexts(_ steps: [CalendarWorkoutProfileBlock]) -> [String] {
        var result: [String] = []
        for text in steps.compactMap(\.text) where !text.isEmpty {
            if result.last != text { result.append(text) }
        }
        return result
    }

    private static func intervalDescription(
        _ label: String,
        _ steps: [CalendarWorkoutProfileBlock],
        ftp: Double?
    ) -> String {
        var watts: [Int] = []
        var percentages: [String] = []
        for step in steps {
            if let value = target(step.targetStart, ftp: ftp), !watts.contains(value) {
                watts.append(value)
            }
            if let value = percent(step.targetStart), !percentages.contains(value) {
                percentages.append(value)
            }
        }
        var result = label
        if !watts.isEmpty {
            result += " at " + watts.map { "\($0) W" }.joined(separator: " / ")
        }
        if !percentages.isEmpty {
            result += " (" + percentages.joined(separator: " / ") + " FTP)"
        }
        return result
    }

    static func duration(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "—" }
        let total = Int(seconds.rounded())
        let minutes = total / 60
        let remainder = total % 60
        if minutes == 0 { return "\(remainder)s" }
        if remainder == 0 { return "\(minutes) min" }
        return "\(minutes)m \(remainder)s"
    }

    static func target(_ fraction: Double?, ftp: Double?) -> Int? {
        guard let fraction, fraction.isFinite,
              let ftp, ftp.isFinite, ftp > 0 else { return nil }
        return Int((fraction * ftp).rounded(.toNearestOrEven))
    }

    static func chartTarget(_ fraction: Double?, ftp: Double?) -> Double? {
        guard let fraction, fraction.isFinite else { return nil }
        guard let ftp, ftp.isFinite, ftp > 0 else { return fraction * 100 }
        return fraction * ftp
    }

    static func percent(_ fraction: Double?) -> String? {
        guard let fraction, fraction.isFinite else { return nil }
        return "\(Int((fraction * 100).rounded()))%"
    }

    /// The desktop varies fill opacity by the same seven power zones. There
    /// is no shared Swift color set, so the chart uses its accent with these
    /// same opacity stops rather than inventing a second hue palette.
    static func zoneOpacity(_ fraction: Double) -> Double {
        switch fraction {
        case ..<0.56: return 0.08
        case ..<0.76: return 0.13
        case ..<0.91: return 0.18
        case ..<1.06: return 0.24
        case ..<1.21: return 0.31
        case ..<1.51: return 0.39
        default: return 0.48
        }
    }
}

/// Field-by-field parsing keeps a malformed optional profile from blanking an
/// otherwise valid calendar page. The publisher bounds this to 512 blocks too.
enum CalendarWorkoutProfileDecoder {
    private static let maxBlocks = 512

    static func blocks(from workout: JSONValue) -> [CalendarWorkoutProfileBlock] {
        guard case let .array(values) = workout["profile"] else { return [] }
        return values.prefix(maxBlocks).compactMap { value in
            guard let start = number(value, key: "start"),
                  let end = number(value, key: "end"),
                  let duration = number(value, key: "duration_s"),
                  let kind = string(value, key: "kind"),
                  start >= 0, end >= start, duration >= 0,
                  end - start == duration else { return nil }
            return CalendarWorkoutProfileBlock(
                start: start,
                end: end,
                durationS: duration,
                targetStart: number(value, key: "target_start"),
                targetEnd: number(value, key: "target_end"),
                kind: kind,
                label: string(value, key: "label"),
                text: string(value, key: "text"),
                free: bool(value, key: "free"),
                segmentIndex: integer(value, key: "segment")
            )
        }
    }

    private static func number(_ value: JSONValue, key: String) -> Double? {
        guard let number = value[key]?.doubleValue, number.isFinite else {
            return nil
        }
        return number
    }

    private static func string(_ value: JSONValue, key: String) -> String? {
        guard let string = value[key]?.stringValue, !string.isEmpty else {
            return nil
        }
        return string
    }

    private static func integer(_ value: JSONValue, key: String) -> Int? {
        guard let number = number(value, key: key), number.rounded() == number else {
            return nil
        }
        return Int(exactly: number)
    }

    private static func bool(_ value: JSONValue, key: String) -> Bool {
        if case let .some(.bool(flag)) = value[key] { return flag }
        return false
    }
}

/// A merged day from one or more `calendar_day` objects.
///
/// The server splits unusually large days into parts. The UI must aggregate by
/// date, rather than replacing one part with another, or a busy day silently
/// loses workouts and rides.
struct CalendarDayEntry: Identifiable, Sendable, Equatable {
    let dateISO: String
    let ooto: Bool
    let phase: String?
    let race: JSONValue?
    let workouts: [JSONValue]
    let activities: [JSONValue]
    let currentFTP: Double?

    var id: String { dateISO }
    var workoutCount: Int { workouts.count }
    var activityCount: Int { activities.count }
    var hasContent: Bool {
        let hasRace: Bool
        if let race {
            if case .null = race {
                hasRace = false
            } else {
                hasRace = true
            }
        } else {
            hasRace = false
        }
        return ooto || phase != nil || hasRace || !workouts.isEmpty || !activities.isEmpty
    }
}

/// The calendar route after its wire objects have been grouped by day.
struct CalendarData: Sendable, Equatable {
    let days: [CalendarDayEntry]
    /// The FTP used to turn a published %FTP target into rider watts. This is
    /// read from the dashboard snapshot, never from a calendar row.
    let currentFTP: Double?
    let source: CloudSnapshot.Source
    let asOf: Date

    init(
        snapshot: CloudSnapshot,
        activitySnapshot: CloudSnapshot? = nil,
        ftp: Double? = nil
    ) {
        struct Accumulator {
            var ooto = false
            var phase: String?
            var race: JSONValue?
            var workouts: [JSONValue] = []
            var activities: [JSONValue] = []
        }

        var grouped: [String: Accumulator] = [:]
        for item in snapshot.items where !item.deleted {
            guard case let .calendarDay(day) = item.payload,
                  let date = day.date,
                  CalendarMonth.fromISODate(date) != nil
            else { continue }
            var value = grouped[date] ?? Accumulator()
            value.ooto = value.ooto || (day.ooto ?? false)
            if value.phase == nil { value.phase = day.phase }
            if value.race == nil { value.race = day.race }
            value.workouts.append(contentsOf: day.workouts ?? [])
            value.activities.append(contentsOf: day.activities ?? [])
            grouped[date] = value
        }

        let activityByID = Self.activitySummaries(from: activitySnapshot)
        days = grouped.keys.sorted().compactMap { date in
            guard let value = grouped[date] else { return nil }
            var activities = value.activities
            var knownActivityIDs = Set(
                activities.compactMap(Self.activityID)
            )
            for workout in value.workouts {
                guard let activityID = Self.completedActivityID(workout),
                      let activity = activityByID[activityID],
                      knownActivityIDs.insert(activityID).inserted
                else { continue }
                activities.append(activity)
            }
            return CalendarDayEntry(
                dateISO: date,
                ooto: value.ooto,
                phase: value.phase,
                race: value.race,
                workouts: value.workouts,
                activities: activities,
                currentFTP: ftp
            )
        }
        source = snapshot.source
        asOf = snapshot.asOf
        currentFTP = ftp
    }

    static func currentFTP(from snapshot: CloudSnapshot?) -> Double? {
        guard let snapshot else { return nil }
        var training: TrainingState?
        var profile: RiderProfile?
        for item in snapshot.items where !item.deleted {
            switch item.payload {
            case let .trainingState(value): training = value
            case let .profile(value): profile = value
            default: break
            }
        }
        for value in [training?.ftp, profile?.resolvedFTP] {
            guard let value, value.isFinite, value > 0 else { continue }
            return value
        }
        return nil
    }

    var hasContent: Bool { days.contains(where: \.hasContent) }

    func entry(for dateISO: String) -> CalendarDayEntry? {
        days.first { $0.dateISO == dateISO }
    }

    func entry(for month: CalendarMonth, day: Int) -> CalendarDayEntry? {
        entry(for: month.isoDate(day: day))
    }

    private static func activitySummaries(
        from snapshot: CloudSnapshot?
    ) -> [Int: JSONValue] {
        guard let snapshot else { return [:] }
        var result: [Int: JSONValue] = [:]
        for item in snapshot.items where !item.deleted {
            guard case let .activity(summary) = item.payload,
                  let id = activityID(summary: summary, itemID: item.id) else {
                continue
            }
            result[id] = activityJSON(summary)
        }
        return result
    }

    private static func activityID(_ value: JSONValue) -> Int? {
        if let number = value["id"]?.doubleValue {
            return Int(exactly: number)
        }
        if let string = value["id"]?.stringValue {
            return Int(string)
        }
        return nil
    }

    private static func activityID(
        summary: ActivitySummary, itemID: String
    ) -> Int? {
        if let id = summary.id, let value = Int(exactly: id) { return value }
        guard let suffix = itemID.split(separator: "-").last else { return nil }
        return Int(suffix)
    }

    private static func completedActivityID(_ workout: JSONValue) -> Int? {
        if let number = workout["completed_activity_id"]?.doubleValue {
            return Int(exactly: number)
        }
        if let string = workout["completed_activity_id"]?.stringValue {
            return Int(string)
        }
        return nil
    }

    private static func activityJSON(_ summary: ActivitySummary) -> JSONValue {
        var fields: [String: JSONValue] = [:]
        if let value = summary.id { fields["id"] = .number(value) }
        if let value = summary.startTime { fields["start_time"] = .string(value) }
        if let value = summary.durationS { fields["duration_s"] = .number(value) }
        if let value = summary.distanceM { fields["distance_m"] = .number(value) }
        if let value = summary.avgPower { fields["avg_power"] = .number(value) }
        if let value = summary.avgHr { fields["avg_hr"] = .number(value) }
        if let value = summary.np { fields["np"] = .number(value) }
        if let value = summary.intensityFactor { fields["if_"] = .number(value) }
        if let value = summary.tss { fields["tss"] = .number(value) }
        if let value = summary.rpe { fields["rpe"] = .number(value) }
        return .object(fields)
    }
}

enum VolumeMetric: String, CaseIterable, Identifiable, Sendable {
    case hours
    case tss
    case distanceKm = "distance_km"
    case calories

    var id: String { rawValue }

    var title: String {
        switch self {
        case .hours: return "Hours"
        case .tss: return "TSS"
        case .distanceKm: return "Distance"
        case .calories: return "Calories"
        }
    }

    var unit: String {
        switch self {
        case .hours: return "h"
        case .tss: return "TSS"
        case .distanceKm: return "km"
        case .calories: return "kcal"
        }
    }

    func value(from week: VolumeWeek) -> Double {
        switch self {
        case .hours: return week.hours ?? 0
        case .tss: return week.tss ?? 0
        case .distanceKm: return week.distanceKm ?? 0
        case .calories: return week.calories ?? 0
        }
    }
}

enum VolumeRange: String, CaseIterable, Identifiable, Sendable {
    case last4
    case last12
    case yearToDate

    var id: String { rawValue }

    var title: String {
        switch self {
        case .last4: return "Last 4 weeks"
        case .last12: return "Last 12 weeks"
        case .yearToDate: return "Year to date"
        }
    }

    var shortTitle: String {
        switch self {
        case .last4: return "Last 4"
        case .last12: return "Last 12"
        case .yearToDate: return "YTD"
        }
    }

    var count: Int? {
        switch self {
        case .last4: return 4
        case .last12: return 12
        case .yearToDate: return nil
        }
    }
}

struct VolumeSummary: Sendable, Equatable {
    let hours: Double
    let tss: Double
    let distanceKm: Double
    let calories: Double

    init(weeks: [VolumeWeek]) {
        hours = weeks.reduce(0) { $0 + ($1.hours ?? 0) }
        tss = weeks.reduce(0) { $0 + ($1.tss ?? 0) }
        distanceKm = weeks.reduce(0) { $0 + ($1.distanceKm ?? 0) }
        calories = weeks.reduce(0) { $0 + ($1.calories ?? 0) }
    }

    func value(for metric: VolumeMetric) -> Double {
        switch metric {
        case .hours: return hours
        case .tss: return tss
        case .distanceKm: return distanceKm
        case .calories: return calories
        }
    }
}

/// Volume objects prepared for the chart and summary tiles.
struct VolumeData: Sendable, Equatable {
    let weeks: [VolumeWeek]
    /// The server omits weeks with no rides. A zero bucket keeps the chart's
    /// spacing and the rolling summaries honest about rest weeks.
    let denseWeeks: [VolumeWeek]
    let source: CloudSnapshot.Source
    let asOf: Date

    init(snapshot: CloudSnapshot) {
        let values = snapshot.items.compactMap { item -> VolumeWeek? in
            guard !item.deleted, case let .volumeWeek(week) = item.payload,
                  let start = week.weekStart, ISODate.parse(start) != nil
            else { return nil }
            return week
        }
        weeks = values.sorted {
            ($0.weekStart ?? "") < ($1.weekStart ?? "")
        }
        denseWeeks = Self.densify(weeks)
        source = snapshot.source
        asOf = snapshot.asOf
    }

    private static func densify(_ weeks: [VolumeWeek]) -> [VolumeWeek] {
        guard let first = weeks.first?.weekStart,
              let last = weeks.last?.weekStart,
              let firstDate = ISODate.parse(first),
              let lastDate = ISODate.parse(last)
        else { return [] }

        var byStart: [String: VolumeWeek] = [:]
        for week in weeks {
            if let start = week.weekStart {
                // A reconciled snapshot normally has one object per week. If
                // an older cache contains duplicates, keep the last one
                // rather than trapping while constructing the chart.
                byStart[start] = week
            }
        }
        var output: [VolumeWeek] = []
        var date = firstDate
        var steps = 0
        while date <= lastDate && steps < 5000 {
            let start = ISODate.format(date)
            output.append(
                byStart[start] ?? VolumeWeek(
                    weekStart: start, hours: 0, tss: 0,
                    distanceKm: 0, calories: 0
                )
            )
            date = CalendarMonth.gregorian.date(byAdding: .day, value: 7, to: date)!
            steps += 1
        }
        return output
    }

    func weeks(
        for range: VolumeRange, referenceDate: Date = Date()
    ) -> [VolumeWeek] {
        let dense = denseWeeks
        guard !dense.isEmpty else { return [] }
        switch range {
        case .last4, .last12:
            return Array(dense.suffix(range.count ?? 0))
        case .yearToDate:
            // Volume is published as Monday buckets. A bucket belongs to the
            // year of its Monday, so the first partial week of a new year is
            // intentionally kept with the prior year's bucket.
            let year = CalendarMonth.current(date: referenceDate).year
            return dense.filter {
                guard let value = $0.weekStart, let date = ISODate.parse(value) else {
                    return false
                }
                return CalendarMonth.gregorian.component(.year, from: date) == year
            }
        }
    }

    func previousWeeks(for range: VolumeRange) -> [VolumeWeek] {
        // Year to date has no fixed-length prior-window comparison.
        guard let count = range.count else { return [] }
        let dense = denseWeeks
        // Both windows must be complete to compare equal spans of history.
        guard dense.count >= count * 2 else { return [] }
        let end = dense.count - count
        let start = end - count
        return Array(dense[start..<end])
    }

    func summary(
        for range: VolumeRange, referenceDate: Date = Date()
    ) -> VolumeSummary {
        VolumeSummary(weeks: weeks(for: range, referenceDate: referenceDate))
    }

    func change(
        for metric: VolumeMetric,
        range: VolumeRange,
        referenceDate: Date = Date()
    ) -> Double? {
        let previous = VolumeSummary(
            weeks: previousWeeks(for: range)
        ).value(for: metric)
        guard previous > 0 else { return nil }
        let current = summary(for: range, referenceDate: referenceDate).value(for: metric)
        return (current - previous) / previous
    }
}

/// Date-only ISO helpers. Parsing is strict so a malformed server object is
/// ignored instead of appearing in an arbitrary month or week.
private enum ISODate {
    private static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = CalendarMonth.gregorian
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    static func parse(_ value: String) -> Date? {
        guard value.count == 10 else { return nil }
        guard let date = formatter.date(from: value), format(date) == value else {
            return nil
        }
        return date
    }

    static func format(_ date: Date) -> String {
        return formatter.string(from: date)
    }
}
