import Foundation

/// A JSON value the app can carry without having a model for it.
///
/// Two places need this and neither is laziness.
///
/// **Forward compatibility.** The delta protocol is at-least-once but never
/// at-least-twice: an object the client fails to store is not resent, because
/// the next `since=` checkpoint has already moved past it.  So an object of a
/// kind this build does not model must still be *kept*, byte-preserving, or a
/// rider who updates the app finds a permanent hole where the objects the old
/// build discarded used to be.  Decoding an unknown kind into this and writing
/// it back out is what makes an app update safe.
///
/// **Sub-payloads the server does not fix.** A calendar day's `workouts` come
/// from `db._plan_workout_row`, whose columns are the desktop schema's, and an
/// activity detail's `zones` is a summary block.  Inventing a Swift struct for
/// either would be a second, weaker copy of a schema issue #163 owns; it would
/// also make a single added column a decode failure for the whole day.
enum JSONValue: Codable, Sendable, Equatable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "not a JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case let .bool(value): try container.encode(value)
        case let .number(value): try container.encode(value)
        case let .string(value): try container.encode(value)
        case let .array(value): try container.encode(value)
        case let .object(value): try container.encode(value)
        }
    }

    // MARK: - Reading

    var doubleValue: Double? {
        if case let .number(value) = self { return value }
        return nil
    }

    var stringValue: String? {
        if case let .string(value) = self { return value }
        return nil
    }

    subscript(key: String) -> JSONValue? {
        if case let .object(fields) = self { return fields[key] }
        return nil
    }
}
