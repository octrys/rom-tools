// NPGameDLL64.dll — client-side GameGuard neutralization stub.
//
// Drop-in for ROMGoldenAge_Data/Plugins/x86_64/NPGameDLL64.dll: the game boots
// to title with no nProtect GameGuard (no GameMon.des, no driver, no .ini
// signature check). GameGuardUnityManager accepts GameGuard as healthy iff
// InitNPGameMon() and CheckNPGameMon() both return NPGAMEMON_SUCCESS (1877);
// every other export is an inert no-op. (Exports take no params: on the Windows
// x64 ABI the caller passes args in registers and cleans up, so ignoring them
// is safe.)
//
//   x86_64-w64-mingw32-gcc -O2 -shared npgamedll64_stub.c -o NPGameDLL64.dll
//       -Wl,--kill-at -static -static-libgcc

#include <windows.h>

#define EXPORT __declspec(dllexport)
#define NPGAMEMON_SUCCESS 1877   // 0x755

// The two gated status calls (+ PreInit, for consistency): report success.
EXPORT unsigned InitNPGameMon(void)     { return NPGAMEMON_SUCCESS; }
EXPORT unsigned CheckNPGameMon(void)    { return NPGAMEMON_SUCCESS; }
EXPORT unsigned PreInitNPGameMonW(void) { return NPGAMEMON_SUCCESS; }
EXPORT unsigned PreInitNPGameMonA(void) { return NPGAMEMON_SUCCESS; }

// String getters: return an empty string, never NULL.
EXPORT const char*    GetGMIStrA(void)    { return ""; }
EXPORT const wchar_t* GetGMIStrW(void)    { return L""; }
EXPORT const char*    GetLastGMIStr(void) { return ""; }

// Everything else: inert, returns 0.
#define NOOP(name) EXPORT unsigned name(void) { return 0; }
NOOP(SetCallbackToGameMon)  NOOP(SetHwndToGameMon)        NOOP(CloseNPGameMon)
NOOP(SendUserIDToGameMonW)  NOOP(SendUserIDToGameMonA)
NOOP(SendCSAuthToGameMon)   NOOP(SendCSAuth2ToGameMon)    NOOP(SendCSAuth3ToGameMon)
NOOP(SendCSAuth2ToGameMonWithSeperator)                   NOOP(GetCSAuth3CallbackData)
NOOP(GetCallbackData)       NOOP(GetInfoFromGameMon)      NOOP(GetInfoFromGameMonEx)
NOOP(GetHackInfoFromGameMon) NOOP(GetGameMonVersion)      NOOP(GetGGMode)
NOOP(GetGGHardwareID)        NOOP(GGGetLastError)         NOOP(GGGetResultCode)
NOOP(GGCheckCodeIntegrity)   NOOP(SetRunGGerror)
NOOP(EncryptPacket)          NOOP(DecryptPacket)          NOOP(EncryptPeerPacket)
NOOP(DecryptPeerPacket)      NOOP(ResourceAuthA)          NOOP(ResourceAuthW)
NOOP(SetUserIDA)             NOOP(SetUserIDW)             NOOP(SetCommonDir)
NOOP(SetModulePathA)         NOOP(SetModulePathW)         NOOP(SetD3DDeviceInfo)
NOOP(CheckD3DDevice)         NOOP(ProtectWDDM)            NOOP(FixVC80DEP)
NOOP(IsAdminPrivilege)       NOOP(NPDect)                 NOOP(NPGuardData)
NOOP(NPReleaseData)

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved) {
    (void)h; (void)reason; (void)reserved;
    return TRUE;
}
