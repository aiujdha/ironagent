// swift-tools-version: 6.2
// Package manifest for the IronAgent macOS companion (menu bar app + IPC library).

import PackageDescription

let package = Package(
    name: "IronAgent",
    platforms: [
        .macOS(.v15),
    ],
    products: [
        .library(name: "IronAgentIPC", targets: ["IronAgentIPC"]),
        .library(name: "IronAgentDiscovery", targets: ["IronAgentDiscovery"]),
        .executable(name: "IronAgent", targets: ["IronAgent"]),
        .executable(name: "ironagent-mac", targets: ["IronAgentMacCLI"]),
    ],
    dependencies: [
        .package(url: "https://github.com/orchetect/MenuBarExtraAccess", exact: "1.2.2"),
        .package(url: "https://github.com/swiftlang/swift-subprocess.git", from: "0.4.0"),
        .package(url: "https://github.com/apple/swift-log.git", from: "1.10.1"),
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.9.0"),
        .package(url: "https://github.com/steipete/Peekaboo.git", branch: "main"),
        .package(path: "../shared/IronAgentKit"),
        .package(path: "../../Swabble"),
    ],
    targets: [
        .target(
            name: "IronAgentIPC",
            dependencies: [],
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .target(
            name: "IronAgentDiscovery",
            dependencies: [
                .product(name: "IronAgentKit", package: "IronAgentKit"),
            ],
            path: "Sources/IronAgentDiscovery",
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .executableTarget(
            name: "IronAgent",
            dependencies: [
                "IronAgentIPC",
                "IronAgentDiscovery",
                .product(name: "IronAgentKit", package: "IronAgentKit"),
                .product(name: "IronAgentChatUI", package: "IronAgentKit"),
                .product(name: "IronAgentProtocol", package: "IronAgentKit"),
                .product(name: "SwabbleKit", package: "swabble"),
                .product(name: "MenuBarExtraAccess", package: "MenuBarExtraAccess"),
                .product(name: "Subprocess", package: "swift-subprocess"),
                .product(name: "Logging", package: "swift-log"),
                .product(name: "Sparkle", package: "Sparkle"),
                .product(name: "PeekabooBridge", package: "Peekaboo"),
                .product(name: "PeekabooAutomationKit", package: "Peekaboo"),
            ],
            exclude: [
                "Resources/Info.plist",
            ],
            resources: [
                .copy("Resources/IronAgent.icns"),
                .copy("Resources/DeviceModels"),
            ],
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .executableTarget(
            name: "IronAgentMacCLI",
            dependencies: [
                "IronAgentDiscovery",
                .product(name: "IronAgentKit", package: "IronAgentKit"),
                .product(name: "IronAgentProtocol", package: "IronAgentKit"),
            ],
            path: "Sources/IronAgentMacCLI",
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
            ]),
        .testTarget(
            name: "IronAgentIPCTests",
            dependencies: [
                "IronAgentIPC",
                "IronAgent",
                "IronAgentDiscovery",
                .product(name: "IronAgentProtocol", package: "IronAgentKit"),
                .product(name: "SwabbleKit", package: "swabble"),
            ],
            swiftSettings: [
                .enableUpcomingFeature("StrictConcurrency"),
                .enableExperimentalFeature("SwiftTesting"),
            ]),
    ])
