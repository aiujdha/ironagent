import Foundation
import Testing
@testable import IronAgent

@Suite(.serialized) struct NodeServiceManagerTests {
    @Test func `builds node service commands with current CLI shape`() async throws {
        try await TestIsolation.withUserDefaultsValues(["ironagent.gatewayProjectRootPath": nil]) {
            let tmp = try makeTempDirForTests()
            CommandResolver.setProjectRoot(tmp.path)

            let ironagentPath = tmp.appendingPathComponent("node_modules/.bin/ironagent")
            try makeExecutableForTests(at: ironagentPath)

            let start = NodeServiceManager._testServiceCommand(["start"])
            #expect(start == [ironagentPath.path, "node", "start", "--json"])

            let stop = NodeServiceManager._testServiceCommand(["stop"])
            #expect(stop == [ironagentPath.path, "node", "stop", "--json"])
        }
    }
}
