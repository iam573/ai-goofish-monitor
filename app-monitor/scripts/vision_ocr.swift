import AppKit
import Foundation
import Vision

func recognize(path: String, index: Int) throws {
    guard let image = NSImage(contentsOfFile: path),
          let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let cgImage = bitmap.cgImage else {
        throw NSError(domain: "QuoteOCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "无法读取图片：\(path)"])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cgImage)
    try handler.perform([request])

    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let clean = candidate.string.replacingOccurrences(of: "\t", with: " ")
        print(String(format: "%d\t%.6f\t%.6f\t%.6f\t%.6f\t%@",
                     index, box.origin.x, box.origin.y,
                     box.size.width, box.size.height, clean))
    }
}

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: vision_ocr <image>...\n".utf8))
    exit(2)
}

for (offset, path) in CommandLine.arguments.dropFirst().enumerated() {
    do {
        try recognize(path: path, index: offset)
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(1)
    }
}
