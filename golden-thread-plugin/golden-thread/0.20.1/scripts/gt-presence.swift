// gt-presence -- the Secure Enclave helper behind gt unlock's Touch ID factor (0.20.1).
//
//   gt-presence available            -> {"secure_enclave", "biometry", "reason"}
//   gt-presence create   {kind, biometric, reason}      -> {"blob", "public"}
//   gt-presence sign     {blob, challenge, reason}      -> {"signature"}   (Touch ID prompt)
//   gt-presence seal     {public, plaintext}            -> {"sealed"}      (no prompt)
//   gt-presence unseal   {blob, sealed, reason}         -> {"plaintext"}   (Touch ID prompt)
//
// JSON on stdin, JSON on stdout; an error is {"error": {"code", "message"}} with exit 1.
// Binary fields are standard base64.
//
// SECURITY INVARIANTS
//  * This helper is NOT trusted. The authority (gt_unlock_touchid.py) verifies every signature
//    in Python against the ENROLLED public key over exactly the challenge it chose, so a helper
//    that answers "ok", or signs something else, proves nothing.
//  * The private keys never leave the Secure Enclave. `blob` is CryptoKit's dataRepresentation:
//    an SE-wrapped handle that only this Mac's Secure Enclave can use, not key material.
//  * Production keys carry [.privateKeyUsage, .biometryCurrentSet]: the SE itself demands a
//    currently enrolled finger for every use, and adding or removing a fingerprint kills the key
//    (re-enrolment is the recovery path, by design). Passcode fallback is NOT allowed.
//  * Nothing secret is written to stderr. Plaintext appears only in the `unseal` stdout JSON,
//    which goes to the authority over a pipe.
//  * biometric:false exists ONLY for the test suite (non-biometric SE keys, so tests never raise a
//    prompt). On that path the LAContext is marked interactionNotAllowed, so even a biometric
//    key handed in by mistake fails instead of prompting.
import CryptoKit
import Foundation
import LocalAuthentication
import Security

struct Fail: Error { let code: String; let message: String }

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func readRequest() throws -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    if data.count > 1 << 20 { throw Fail(code: "malformed", message: "request too large") }
    if data.isEmpty || data.allSatisfy({ $0 == 0x20 || $0 == 0x0a || $0 == 0x0d || $0 == 0x09 }) { return [:] }
    guard let obj = try? JSONSerialization.jsonObject(with: data), let d = obj as? [String: Any] else {
        throw Fail(code: "malformed", message: "stdin is not a JSON object")
    }
    return d
}

func b64(_ req: [String: Any], _ key: String) throws -> Data {
    guard let s = req[key] as? String, let d = Data(base64Encoded: s) else {
        throw Fail(code: "malformed", message: "\(key) must be base64")
    }
    return d
}

func reasonOf(_ req: [String: Any]) throws -> String {
    guard let r = req["reason"] as? String, !r.isEmpty, r.count <= 500 else {
        throw Fail(code: "malformed", message: "reason (the prompt text) is required")
    }
    return r
}

func biometricOf(_ req: [String: Any]) -> Bool { (req["biometric"] as? Bool) ?? true }

// LocalAuthentication errors -> the factor's codes. Anything else is helper_failed.
func mapLA(_ error: Error) -> Fail {
    let ns = error as NSError
    if ns.domain == LAErrorDomain, let code = LAError.Code(rawValue: ns.code) {
        switch code {
        case .userCancel, .appCancel, .systemCancel, .userFallback:
            return Fail(code: "cancelled", message: "the Touch ID prompt was cancelled")
        case .biometryLockout:
            return Fail(code: "locked_out", message: "Touch ID is locked out; unlock the Mac with its password first")
        case .authenticationFailed:
            return Fail(code: "wrong", message: "the fingerprint was not recognised")
        case .biometryNotAvailable, .biometryNotEnrolled, .passcodeNotSet, .notInteractive:
            return Fail(code: "unavailable", message: "Touch ID is not available (\(code.rawValue))")
        default:
            return Fail(code: "helper_failed", message: "LocalAuthentication error \(code.rawValue)")
        }
    }
    return Fail(code: "helper_failed", message: "\(ns.domain) error \(ns.code)")
}

// One LAContext, evaluated ONCE with the authority's reason (which names the requester and the
// scope), then handed to CryptoKit as the key's authenticationContext so the SE operation reuses
// that single authentication instead of raising a second, reason-less prompt.
func authContext(reason: String, biometric: Bool) throws -> LAContext {
    let ctx = LAContext()
    ctx.localizedReason = reason
    if !biometric {
        ctx.interactionNotAllowed = true          // test keys: never prompt, fail instead
        return ctx
    }
    var result: Result<Void, Error> = .failure(Fail(code: "timeout", message: "no answer from Touch ID"))
    let done = DispatchSemaphore(value: 0)
    ctx.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { ok, err in
        result = ok ? .success(()) : .failure(err ?? Fail(code: "helper_failed", message: "evaluation failed"))
        done.signal()
    }
    if done.wait(timeout: .now() + 115) == .timedOut {
        ctx.invalidate()
        throw Fail(code: "timeout", message: "nobody answered the Touch ID prompt")
    }
    if case .failure(let e) = result { throw (e as? Fail) ?? mapLA(e) }
    return ctx
}

func accessControl(biometric: Bool) throws -> SecAccessControl {
    var err: Unmanaged<CFError>?
    let flags: SecAccessControlCreateFlags = biometric ? [.privateKeyUsage, .biometryCurrentSet] : [.privateKeyUsage]
    guard let ac = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, flags, &err) else {
        throw Fail(code: "helper_failed", message: "could not build the access control")
    }
    return ac
}

let sealInfo = Data("gt-seal-v1".utf8)

// HKDF salt binds both public keys, so a box cannot be re-pointed at another recipient key.
func wrapKey(_ shared: SharedSecret, eph: Data, recipient: Data) -> SymmetricKey {
    shared.hkdfDerivedSymmetricKey(using: SHA256.self, salt: eph + recipient, sharedInfo: sealInfo, outputByteCount: 32)
}

func run(_ cmd: String) throws -> [String: Any] {
    let req = try readRequest()
    switch cmd {
    case "available":
        let ctx = LAContext()
        var err: NSError?
        let bio = ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err)
        let se = SecureEnclave.isAvailable
        let reason = !se ? "this Mac has no Secure Enclave"
            : bio ? "Secure Enclave and Touch ID are available"
            : "Touch ID cannot be used now: \(err.map { mapLA($0).message } ?? "unknown")"
        return ["secure_enclave": se, "biometry": bio, "reason": reason]

    case "create":
        guard SecureEnclave.isAvailable else { throw Fail(code: "unavailable", message: "no Secure Enclave") }
        let ac = try accessControl(biometric: biometricOf(req))
        let ctx = LAContext()
        ctx.localizedReason = (req["reason"] as? String) ?? "create a gt unlock key"
        ctx.interactionNotAllowed = true          // creating a key never needs a prompt
        switch req["kind"] as? String {
        case "sign":
            let k = try SecureEnclave.P256.Signing.PrivateKey(accessControl: ac, authenticationContext: ctx)
            return ["blob": k.dataRepresentation.base64EncodedString(), "public": k.publicKey.x963Representation.base64EncodedString()]
        case "agree":
            let k = try SecureEnclave.P256.KeyAgreement.PrivateKey(accessControl: ac, authenticationContext: ctx)
            return ["blob": k.dataRepresentation.base64EncodedString(), "public": k.publicKey.x963Representation.base64EncodedString()]
        default:
            throw Fail(code: "malformed", message: "kind is sign or agree")
        }

    case "sign":
        let blob = try b64(req, "blob"), challenge = try b64(req, "challenge")
        let ctx = try authContext(reason: try reasonOf(req), biometric: biometricOf(req))
        let key: SecureEnclave.P256.Signing.PrivateKey
        do { key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: ctx) }
        catch { throw Fail(code: "not_enrolled", message: "the enrolled Touch ID key no longer opens (fingerprints changed?); re-enrol") }
        do { return ["signature": try key.signature(for: challenge).derRepresentation.base64EncodedString()] }
        catch { throw mapLA(error) }

    case "seal":
        let recipient = try b64(req, "public"), plaintext = try b64(req, "plaintext")
        let pub: P256.KeyAgreement.PublicKey
        do { pub = try P256.KeyAgreement.PublicKey(x963Representation: recipient) }
        catch { throw Fail(code: "malformed", message: "public is not a P-256 point") }
        let eph = P256.KeyAgreement.PrivateKey()
        let ephPub = eph.publicKey.x963Representation
        let key = wrapKey(try eph.sharedSecretFromKeyAgreement(with: pub), eph: ephPub, recipient: pub.x963Representation)
        guard let box = try AES.GCM.seal(plaintext, using: key).combined else {
            throw Fail(code: "helper_failed", message: "AES-GCM produced no box")
        }
        return ["sealed": (ephPub + box).base64EncodedString()]

    case "unseal":
        let blob = try b64(req, "blob"), sealed = try b64(req, "sealed")
        guard sealed.count >= 65 + 12 + 16 else { throw Fail(code: "malformed", message: "sealed value is too short") }
        let ephPubData = sealed.prefix(65), box = sealed.dropFirst(65)
        let ephPub: P256.KeyAgreement.PublicKey
        do { ephPub = try P256.KeyAgreement.PublicKey(x963Representation: ephPubData) }
        catch { throw Fail(code: "malformed", message: "sealed value is damaged") }
        let ctx = try authContext(reason: try reasonOf(req), biometric: biometricOf(req))
        let key: SecureEnclave.P256.KeyAgreement.PrivateKey
        do { key = try SecureEnclave.P256.KeyAgreement.PrivateKey(dataRepresentation: blob, authenticationContext: ctx) }
        catch { throw Fail(code: "not_enrolled", message: "the enrolled Touch ID key no longer opens (fingerprints changed?); re-enrol") }
        let shared: SharedSecret
        do { shared = try key.sharedSecretFromKeyAgreement(with: ephPub) } catch { throw mapLA(error) }
        let wk = wrapKey(shared, eph: Data(ephPubData), recipient: key.publicKey.x963Representation)
        do {
            let plain = try AES.GCM.open(try AES.GCM.SealedBox(combined: Data(box)), using: wk)
            return ["plaintext": plain.base64EncodedString()]
        } catch { throw Fail(code: "malformed", message: "sealed value is damaged or sealed to another key") }

    default:
        throw Fail(code: "malformed", message: "commands: available create sign seal unseal")
    }
}

let args = CommandLine.arguments
do {
    guard args.count == 2 else { throw Fail(code: "malformed", message: "usage: gt-presence available|create|sign|seal|unseal < request.json") }
    emit(try run(args[1]))
    exit(0)
} catch let f as Fail {
    emit(["error": ["code": f.code, "message": f.message]])
    exit(1)
} catch {
    emit(["error": ["code": "helper_failed", "message": "\((error as NSError).domain) error \((error as NSError).code)"]])
    exit(1)
}
