// Masks of the person in each frame, with Apple's Vision person segmentation (macOS 12+), for
// `splat-studio` --person (masks.py make_person_masks). No model to download: it ships with macOS.
//   swift person_mask.swift OUT_DIR frame1.jpg frame2.jpg ...
// writes OUT_DIR/<frame name>.png, 8-bit grey, the frame's size: 255 on the person, 0 elsewhere.
import AppKit
import CoreImage
import Foundation
import Vision

let args = CommandLine.arguments
guard args.count >= 3 else {
  FileHandle.standardError.write("usage: person_mask.swift OUT_DIR FRAME...\n".data(using: .utf8)!)
  exit(2)
}
let out = URL(fileURLWithPath: args[1])
let context = CIContext()
var failed = 0
for (i, path) in args.dropFirst(2).enumerated() {
  let url = URL(fileURLWithPath: path)
  guard let image = CIImage(contentsOf: url) else { failed += 1; continue }
  let request = VNGeneratePersonSegmentationRequest()
  request.qualityLevel = .accurate
  request.outputPixelFormat = kCVPixelFormatType_OneComponent8
  do {
    try VNImageRequestHandler(ciImage: image, options: [:]).perform([request])
  } catch { failed += 1; continue }
  guard let buffer = request.results?.first?.pixelBuffer else { failed += 1; continue }
  // the mask comes smaller than the frame: scaled to it, then cut at half (black or white)
  var mask = CIImage(cvPixelBuffer: buffer)
  mask = mask.transformed(by: CGAffineTransform(scaleX: image.extent.width / mask.extent.width,
                                                y: image.extent.height / mask.extent.height))
  mask = mask.applyingFilter("CIColorThreshold", parameters: ["inputThreshold": 0.5])
  let name = url.deletingPathExtension().lastPathComponent + ".png"
  guard let cg = context.createCGImage(mask, from: image.extent, format: .L8,
                                       colorSpace: CGColorSpaceCreateDeviceGray()) else { failed += 1; continue }
  let rep = NSBitmapImageRep(cgImage: cg)
  guard let png = rep.representation(using: .png, properties: [:]) else { failed += 1; continue }
  try? png.write(to: out.appendingPathComponent(name))
  print("mask \(i + 1)/\(args.count - 2)")
  fflush(stdout)
}
exit(failed == 0 ? 0 : 1)
