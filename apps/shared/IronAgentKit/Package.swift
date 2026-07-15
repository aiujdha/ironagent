// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "IronAgentKit",
    platforms: [
        .iOS(.v18),
        .macOS(.v15),
    ],
    products: [
        .library(name: "IronAgentProtocol", targets: ["IronAgentProtocol"]),
        .library(name: "IronAgentKit", targets: ["IronAgentKit"]),
        .library(name: "IronAgentChatUI", targets: ["IronAgentChatUI"]),
    ],
    dependencies: [
        .package(url: "https://github.com/steipete/ElevenLabsKit", exact: "0.1.0"),
        .package(url: "https://github.com/gonzalezreal/textual", exact: "0.3.1"),
    ],
    targets: [
        .target(
            name: "IronAgentProtocol",
            path: "Sources/IronAgentProtocol",
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .target(
            name: "IronAgentKit",
            dependencies: [
                "IronAgentProtocol",
                .product(name: "ElevenLabsKit", package: "ElevenLabsKit"),
            ],
            path: "Sources/IronAgentKit",
            resources: [
                .process("Resources"),
            ],
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .target(
            name: "IronAgentChatUI",
            dependencies: [
                "IronAgentKit",
                .product(
                    name: "Textual",
                    package: "textual",
                    condition: .when(platforms: [.macOS, .iOS])),
            ],
            path: "Sources/IronAgentChatUI",
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .testTarget(
            name: "IronAgentKitTests",
            dependencies: ["IronAgentKit", "IronAgentChatUI"],
            path: "Tests/IronAgentKitTests",
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
                .enableExperimentalFeature("SwiftTesting"),
            ]),
    ])
