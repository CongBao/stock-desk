import AppKit
import Foundation
import Vision

func fail(_ message: String) -> Never {
    fputs("macOS visual analyzer failed: \(message)\n", stderr)
    exit(1)
}

guard CommandLine.arguments.count == 2 else {
    fail("usage: macos-visual-analyzer SCREENSHOT.png")
}

let screenshotPath = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: screenshotPath) else {
    fail("screenshot is unreadable")
}
var proposedRect = NSRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &proposedRect, context: nil, hints: nil) else {
    fail("screenshot has no bitmap representation")
}

let textRequest = VNRecognizeTextRequest()
textRequest.recognitionLevel = .accurate
textRequest.recognitionLanguages = ["zh-Hans", "en-US"]
textRequest.usesLanguageCorrection = false
do {
    try VNImageRequestHandler(cgImage: cgImage).perform([textRequest])
} catch {
    fail("text recognition failed")
}
let recognizedText = (textRequest.results ?? []).compactMap {
    $0.topCandidates(1).first?.string
}

let bitmap = NSBitmapImageRep(cgImage: cgImage)
let sampleColumns = min(64, bitmap.pixelsWide)
let sampleRows = min(64, bitmap.pixelsHigh)
guard sampleColumns > 0, sampleRows > 0 else {
    fail("screenshot dimensions are invalid")
}
var luminance: [Double] = []
luminance.reserveCapacity(sampleColumns * sampleRows)
for row in 0 ..< sampleRows {
    let y = min(bitmap.pixelsHigh - 1, row * bitmap.pixelsHigh / sampleRows)
    for column in 0 ..< sampleColumns {
        let x = min(bitmap.pixelsWide - 1, column * bitmap.pixelsWide / sampleColumns)
        guard let color = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.sRGB) else {
            continue
        }
        luminance.append(
            0.2126 * color.redComponent
                + 0.7152 * color.greenComponent
                + 0.0722 * color.blueComponent
        )
    }
}
guard !luminance.isEmpty else {
    fail("screenshot has no readable color samples")
}
luminance.sort()
let middle = luminance.count / 2
let medianLuminance = luminance.count.isMultiple(of: 2)
    ? (luminance[middle - 1] + luminance[middle]) / 2
    : luminance[middle]

let result: [String: Any] = [
    "recognized_text": recognizedText,
    "median_luminance": medianLuminance,
]
do {
    let encoded = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(encoded)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("result serialization failed")
}
