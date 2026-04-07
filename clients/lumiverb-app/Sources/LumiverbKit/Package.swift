// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LumiverbKit",
    platforms: [
        .macOS(.v14),
        .iOS(.v17),
    ],
    products: [
        .library(name: "LumiverbKit", targets: ["LumiverbKit"]),
    ],
    targets: [
        .target(
            name: "LumiverbKit",
            path: "Sources/LumiverbKit"
        ),
        .testTarget(
            name: "LumiverbKitTests",
            dependencies: ["LumiverbKit"],
            path: "Tests/LumiverbKitTests",
            resources: [
                .copy("Fixtures"),
            ]
        ),
    ]
)
