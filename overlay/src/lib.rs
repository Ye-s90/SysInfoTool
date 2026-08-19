//! SysInfoTool game overlay DLL.
//!
//! Injected into the game process. Hooks IDXGISwapChain::Present and draws
//! the monitoring text (read from a shared memory file written by the main
//! SysInfoTool process) onto the game frame.
//!
//! Phase B1: shared-memory reading + D3D11 Present hook (vtable patch).
//! Rendering is added in B2.
//!
//! All Win32 / D3D11 calls are hand-written FFI (no third-party crates).
//! Build: rustup run stable-x86_64-pc-windows-gnu rustc --crate-type cdylib ...

#![allow(non_snake_case)]
#![allow(clippy::missing_safety_doc)]

use std::ffi::c_void;
use std::ptr;

// ---------------------------------------------------------------------------
// Hand-written FFI
// ---------------------------------------------------------------------------
extern "system" {
    fn CreateThread(
        lpattr: *const c_void,
        dwstack: usize,
        lpstart: extern "system" fn(*mut c_void) -> u32,
        lparg: *mut c_void,
        dwflags: u32,
        lpthreadid: *mut u32,
    ) -> *mut c_void;
    fn CloseHandle(h: *mut c_void) -> i32;
    fn LoadLibraryW(lp: *const u16) -> *mut c_void;
    fn GetProcAddress(h: *mut c_void, name: *const u8) -> *mut c_void;
    fn OpenFileMappingW(desired: u32, inherit: i32, name: *const u16) -> *mut c_void;
    fn MapViewOfFile(h: *mut c_void, access: u32, off_hi: u32, off_lo: u32, bytes: usize) -> *mut c_void;
    fn UnmapViewOfFile(p: *const c_void) -> i32;
    fn VirtualProtect(addr: *mut c_void, size: usize, new_prot: u32, old_prot: *mut u32) -> i32;
    fn GetLastError() -> u32;
}

#[link(name = "user32")]
extern "system" {
    fn GetDesktopWindow() -> *mut c_void;
}

const DLL_PROCESS_ATTACH: u32 = 1;
const FILE_MAP_READ: u32 = 0x0004;
const PAGE_EXECUTE_READWRITE: u32 = 0x40;
const SHM_SIZE: usize = 4096;
const SHM_NAME: &str = r"Local\SysInfoToolOverlay";
// Test process writes (swapchain*, device*, ctx*) to this mapping so the
// overlay worker can hook the game's real swapchain with its matching device.
const SHM_DEV_SIZE: usize = 24;
const SHM_NAME_DEV: &str = r"Local\SysInfoToolDev";

const fn wide(s: &str) -> [u16; 64] {
    let b = s.as_bytes();
    let mut out = [0u16; 64];
    let mut i = 0;
    while i < b.len() && i < 63 {
        out[i] = b[i] as u16;
        i += 1;
    }
    out[i] = 0;
    out
}

static mut ORIG_PRESENT: usize = 0;
static mut SHM_TEXT: [u8; SHM_SIZE] = [0; SHM_SIZE];
static mut SHM_READY: bool = false;

/// DllMain entry: spawn a worker thread (no heavy work under loader lock).
#[no_mangle]
pub extern "system" fn DllMain(_inst: *mut c_void, reason: u32, _reserved: *mut c_void) -> i32 {
    if reason == DLL_PROCESS_ATTACH {
        unsafe {
            let h = CreateThread(ptr::null(), 0, worker, ptr::null_mut(), 0, ptr::null_mut());
            if !h.is_null() {
                CloseHandle(h);
            }
        }
    }
    1
}

// ---------------------------------------------------------------------------
// Shared memory
// ---------------------------------------------------------------------------
fn read_shared_memory() -> &'static [u8] {
    unsafe {
        if !SHM_READY {
            let name = wide(SHM_NAME);
            let h = OpenFileMappingW(FILE_MAP_READ, 0, name.as_ptr());
            if h.is_null() {
                let _ = std::fs::write(
                    r"C:\Users\YeTom\WorkBuddy\2026-08-05-22-32-13\overlay_shm_log.txt",
                    format!("OpenFileMappingW failed err={}", GetLastError()),
                );
                return &[];
            }
            let p = MapViewOfFile(h, FILE_MAP_READ, 0, 0, SHM_SIZE);
            if p.is_null() {
                CloseHandle(h);
                return &[];
            }
            ptr::copy_nonoverlapping(p as *const u8, SHM_TEXT.as_mut_ptr(), SHM_SIZE);
            UnmapViewOfFile(p);
            CloseHandle(h);
            SHM_READY = true;
        }
        let end = SHM_TEXT.iter().position(|&b| b == 0).unwrap_or(SHM_SIZE);
        &SHM_TEXT[..end]
    }
}

// ---------------------------------------------------------------------------
// D3D11 (dynamic load, minimal structs)
// ---------------------------------------------------------------------------
#[repr(C)]
struct DXGI_RATIONAL {
    numerator: u32,
    denominator: u32,
}

#[repr(C)]
struct DXGI_MODE_DESC {
    width: u32,
    height: u32,
    refresh: DXGI_RATIONAL,
    format: u32, // DXGI_FORMAT_R8G8B8A8_UNORM = 28
    scanline: u32,
    scaling: u32,
}

#[repr(C)]
struct DXGI_SAMPLE_DESC {
    count: u32,
    quality: u32,
}

#[repr(C)]
struct DXGI_SWAP_CHAIN_DESC {
    buffer_desc: DXGI_MODE_DESC,
    sample_desc: DXGI_SAMPLE_DESC,
    buffer_usage: u32,
    buffer_count: u32,
    output_window: *mut c_void, // HWND
    windowed: i32,
    swap_effect: u32,
    flags: u32,
}

type D3D11CreateDeviceAndSwapChainFn = unsafe extern "system" fn(
    p_adapter: *mut c_void,
    driver_type: u32,
    software: *mut c_void,
    flags: u32,
    p_feature_levels: *const u32,
    feature_levels: u32,
    sdk_version: u32,
    p_desc: *const DXGI_SWAP_CHAIN_DESC,
    pp_swap_chain: *mut *mut c_void,
    pp_device: *mut *mut c_void,
    pp_ctx: *mut *mut c_void,
    p_feature_level: *mut u32,
) -> i32;

fn load_d3d11_create() -> Option<D3D11CreateDeviceAndSwapChainFn> {
    unsafe {
        let dll = LoadLibraryW(wide(r"d3d11.dll").as_ptr());
        if dll.is_null() {
            return None;
        }
        let p = GetProcAddress(dll, b"D3D11CreateDeviceAndSwapChain\0".as_ptr());
        Some(std::mem::transmute::<*mut c_void, D3D11CreateDeviceAndSwapChainFn>(p))
    }
}

/// Create a dummy device + swapchain just to obtain the vtable layout.
fn create_dummy_swapchain() -> Option<(*mut c_void, *mut c_void, *mut c_void)> {
    unsafe {
        let create = load_d3d11_create()?;
        let desc = DXGI_SWAP_CHAIN_DESC {
            buffer_desc: DXGI_MODE_DESC {
                width: 4,
                height: 4,
                refresh: DXGI_RATIONAL { numerator: 60, denominator: 1 },
                format: 28,
                scanline: 0,
                scaling: 0,
            },
            sample_desc: DXGI_SAMPLE_DESC { count: 1, quality: 0 },
            buffer_usage: 0x20, // DXGI_USAGE_RENDER_TARGET_OUTPUT
            buffer_count: 1,
            output_window: GetDesktopWindow(),
            windowed: 1,
            swap_effect: 0, // DISCARD, matches most games (FLIP uses a different vtable)
            flags: 0,
        };
        let mut swapchain: *mut c_void = ptr::null_mut();
        let mut device: *mut c_void = ptr::null_mut();
        let mut ctx: *mut c_void = ptr::null_mut();
        let lvl = [0xb000u32, 0xa000]; // D3D_FEATURE_LEVEL_11_0 / 10_0
        let hr = create(
            ptr::null_mut(), 0x01, ptr::null_mut(), 0, lvl.as_ptr(), 2, 7,
            &desc, &mut swapchain, &mut device, &mut ctx, ptr::null_mut(),
        );
        if hr < 0 || swapchain.is_null() {
            return None;
        }
        Some((swapchain, device, ctx))
    }
}

/// Hook IDXGISwapChain::Present (vtable slot 8) -> our function.
fn hook_present(swapchain: *mut c_void) -> bool {
    unsafe {
        let vtbl = *(swapchain as *const *const usize);
        let slot = vtbl.add(8) as *mut usize;
        let mut old_prot: u32 = 0;
        if VirtualProtect(slot as *mut c_void, std::mem::size_of::<usize>(), PAGE_EXECUTE_READWRITE, &mut old_prot) == 0 {
            return false;
        }
        ORIG_PRESENT = *slot;
        *slot = hooked_present as usize;
        VirtualProtect(slot as *mut c_void, std::mem::size_of::<usize>(), old_prot, &mut old_prot);
        true
    }
}

type PresentFn = unsafe extern "system" fn(*mut c_void, u32, u32) -> i32;

// ===========================================================================
// B2: text rendering (D3D11)
// ===========================================================================
const TEX_W: u32 = 1024;
const TEX_H: u32 = 64;
const CHAR_W: u32 = 8;
const CHAR_H: u32 = 8;
const SCALE: u32 = 2; // 8x8 font drawn at 16x16

// D3D11 vtable indices. IMPORTANT: empirically this system's d3d11.dll
// device/context vtable = official indices - 4 (no ID3D11DeviceChild layer).
// Verified by experiment: CreateBuffer=3, CreateTexture2D=5,
// CreateShaderResourceView=7, CreateSamplerState=23 all return S_OK, while
// official indices (7/9/11/27) crash or fail. SwapChain (DXGI) matches the
// official layout (Present=8, GetBuffer=9, GetDesc=12 verified).
const DEV_CREATE_BUFFER: usize = 3;
const DEV_CREATE_TEXTURE2D: usize = 5;
const DEV_CREATE_SRV: usize = 7;
const DEV_CREATE_RTV: usize = 9;
const DEV_CREATE_INPUT_LAYOUT: usize = 11;
const DEV_CREATE_VERTEX_SHADER: usize = 12;
const DEV_CREATE_PIXEL_SHADER: usize = 15;
const DEV_CREATE_BLEND_STATE: usize = 20;
const DEV_CREATE_SAMPLER_STATE: usize = 23;
// Context vtable indices (same -4 offset from official)
const CTX_PSSET_SHADER_RESOURCES: usize = 4;
const CTX_PSSET_SHADER: usize = 5;
const CTX_PSSET_SAMPLERS: usize = 6;
const CTX_VSSET_SHADER: usize = 7;
const CTX_DRAW: usize = 9;
const CTX_IASET_INPUT_LAYOUT: usize = 13;
const CTX_IASET_VERTEX_BUFFERS: usize = 14;
const CTX_IASET_PRIMITIVE_TOPOLOGY: usize = 20;
const CTX_OMSET_RENDER_TARGETS: usize = 29;
const CTX_OMSET_BLEND_STATE: usize = 31;
const CTX_RSSET_VIEWPORTS: usize = 38;
const CTX_UPDATE_SUBRESOURCE: usize = 42;
// SwapChain vtable (DXGI has no DeviceChild layer, official verified)
const SC_GET_BUFFER: usize = 9;
const SC_GET_DESC: usize = 12;

const D3D11_BIND_RENDER_TARGET: u32 = 0x2;
const D3D11_BIND_SHADER_RESOURCE: u32 = 0x8;
const D3D11_USAGE_DYNAMIC: u32 = 2;
const D3D11_USAGE_DEFAULT: u32 = 0;
const D3D11_CPU_ACCESS_WRITE: u32 = 0x10000;
const D3D11_RESOURCE_MISC_NONE: u32 = 0;
const DXGI_FORMAT_R8G8B8A8_UNORM: u32 = 28;
const D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST: u32 = 4;
const D3D_BLEND_ONE: u32 = 2;
const D3D_BLEND_INV_SRC_ALPHA: u32 = 5;
const D3D_BLEND_SRC_ALPHA: u32 = 7;
const D3D_BLEND_OP_ADD: u32 = 1;

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Vertex {
    pos: [f32; 2],
    uv: [f32; 2],
}

#[repr(C)]
struct D3D11_TEXTURE2D_DESC {
    width: u32,
    height: u32,
    mip_levels: u32,
    array_size: u32,
    format: u32,
    sample_desc: DXGI_SAMPLE_DESC,
    usage: u32,
    bind_flags: u32,
    cpu_access: u32,
    misc_flags: u32,
}

#[repr(C)]
struct D3D11_SUBRESOURCE_DATA {
    p_sys_mem: *const c_void,
    sys_mem_pitch: u32,
    sys_mem_slice_pitch: u32,
}

#[repr(C)]
struct D3D11_BUFFER_DESC {
    byte_width: u32,
    usage: u32,
    bind_flags: u32,
    cpu_access: u32,
    misc_flags: u32,
    structure_byte_stride: u32,
}

#[repr(C)]
struct D3D11_VIEWPORT {
    top_left_x: f32,
    top_left_y: f32,
    width: f32,
    height: f32,
    min_depth: f32,
    max_depth: f32,
}

#[repr(C)]
struct D3D11_BLEND_DESC {
    alpha_to_coverage_enable: i32,
    independent_blend_enable: i32,
    render_target: [D3D11_RENDER_TARGET_BLEND_DESC; 8],
}

#[repr(C)]
#[derive(Clone, Copy)]
struct D3D11_RENDER_TARGET_BLEND_DESC {
    blend_enable: i32,
    src_blend: u32,
    dest_blend: u32,
    blend_op: u32,
    src_blend_alpha: u32,
    dest_blend_alpha: u32,
    blend_op_alpha: u32,
    render_target_write_mask: u8,
}

#[repr(C)]
struct D3D11_SAMPLER_DESC {
    filter: u32,
    address_u: u32,
    address_v: u32,
    address_w: u32,
    mip_lod_bias: f32,
    max_anisotropy: u32,
    comparison_func: u32,
    border_color: [f32; 4],
    min_lod: f32,
    max_lod: f32,
}

#[repr(C)]
struct D3D11_INPUT_ELEMENT_DESC {
    semantic_name: *const u8,
    semantic_index: u32,
    format: u32,
    input_slot: u32,
    aligned_byte_offset: u32,
    input_slot_class: u32,
    instance_data_step_rate: u32,
}

type CreateTex2DFn = unsafe extern "system" fn(*mut c_void, *const D3D11_TEXTURE2D_DESC, *const D3D11_SUBRESOURCE_DATA, *mut *mut c_void) -> i32;
type CreateSRVFn = unsafe extern "system" fn(*mut c_void, *mut c_void, *const c_void, *mut *mut c_void) -> i32;
type CreateRTVFn = unsafe extern "system" fn(*mut c_void, *mut c_void, *const c_void, *mut *mut c_void) -> i32;
type CreateInputLayoutFn = unsafe extern "system" fn(*mut c_void, *const D3D11_INPUT_ELEMENT_DESC, u32, *const u8, usize, *mut *mut c_void) -> i32;
type CreateShaderFn = unsafe extern "system" fn(*mut c_void, *const u8, usize, *mut c_void, *mut *mut c_void) -> i32;
type CreateBlendFn = unsafe extern "system" fn(*mut c_void, *const D3D11_BLEND_DESC, *mut *mut c_void) -> i32;
type CreateSamplerFn = unsafe extern "system" fn(*mut c_void, *const D3D11_SAMPLER_DESC, *mut *mut c_void) -> i32;
type CreateBufferFn = unsafe extern "system" fn(*mut c_void, *const D3D11_BUFFER_DESC, *const D3D11_SUBRESOURCE_DATA, *mut *mut c_void) -> i32;

static mut RENDER_OK: bool = false;
static mut R_DEVICE: *mut c_void = ptr::null_mut();
static mut R_CTX: *mut c_void = ptr::null_mut();
static mut R_TEX: *mut c_void = ptr::null_mut();
static mut R_SRV: *mut c_void = ptr::null_mut();
static mut R_VB: *mut c_void = ptr::null_mut();
static mut R_VS: *mut c_void = ptr::null_mut();
static mut R_PS: *mut c_void = ptr::null_mut();
static mut R_IL: *mut c_void = ptr::null_mut();
static mut R_BLEND: *mut c_void = ptr::null_mut();
static mut R_SAMPLER: *mut c_void = ptr::null_mut();
static mut R_BACKBUF: *mut c_void = ptr::null_mut();
static mut R_RTV: *mut c_void = ptr::null_mut();
static mut R_BACK_W: u32 = 0;
static mut R_BACK_H: u32 = 0;

// 8x8 font subset: " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ%|.:-/"
const FONT_CHARS: &[u8] = b" 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ%|.:-/";
const FONT_DATA: &[u8] = &[
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, // ' '
    0x3C,0x66,0x6E,0x76,0x66,0x66,0x3C,0x00, // '0'
    0x18,0x38,0x18,0x18,0x18,0x18,0x7E,0x00, // '1'
    0x3C,0x66,0x06,0x0C,0x18,0x30,0x7E,0x00, // '2'
    0x3C,0x66,0x06,0x1C,0x06,0x66,0x3C,0x00, // '3'
    0x0C,0x1C,0x3C,0x6C,0x7E,0x0C,0x0C,0x00, // '4'
    0x7E,0x60,0x7C,0x06,0x06,0x66,0x3C,0x00, // '5'
    0x3C,0x66,0x60,0x7C,0x66,0x66,0x3C,0x00, // '6'
    0x7E,0x66,0x0C,0x18,0x18,0x18,0x18,0x00, // '7'
    0x3C,0x66,0x66,0x3C,0x66,0x66,0x3C,0x00, // '8'
    0x3C,0x66,0x66,0x3E,0x06,0x66,0x3C,0x00, // '9'
    0x3C,0x66,0x66,0x7E,0x66,0x66,0x66,0x00, // 'A'
    0x7C,0x66,0x66,0x7C,0x66,0x66,0x7C,0x00, // 'B'
    0x3C,0x66,0x60,0x60,0x60,0x66,0x3C,0x00, // 'C'
    0x78,0x6C,0x66,0x66,0x66,0x6C,0x78,0x00, // 'D'
    0x7E,0x60,0x60,0x78,0x60,0x60,0x7E,0x00, // 'E'
    0x7E,0x60,0x60,0x78,0x60,0x60,0x60,0x00, // 'F'
    0x3C,0x66,0x60,0x6E,0x66,0x66,0x3C,0x00, // 'G'
    0x66,0x66,0x66,0x7E,0x66,0x66,0x66,0x00, // 'H'
    0x7E,0x18,0x18,0x18,0x18,0x18,0x7E,0x00, // 'I'
    0x1E,0x0C,0x0C,0x0C,0x6C,0x6C,0x38,0x00, // 'J'
    0x66,0x6C,0x78,0x70,0x78,0x6C,0x66,0x00, // 'K'
    0x60,0x60,0x60,0x60,0x60,0x60,0x7E,0x00, // 'L'
    0x66,0x77,0x7F,0x7F,0x6B,0x63,0x63,0x00, // 'M'
    0x66,0x76,0x7E,0x6E,0x66,0x66,0x66,0x00, // 'N'
    0x3C,0x66,0x66,0x66,0x66,0x66,0x3C,0x00, // 'O'
    0x7C,0x66,0x66,0x7C,0x60,0x60,0x60,0x00, // 'P'
    0x3C,0x66,0x66,0x66,0x6E,0x3C,0x0E,0x00, // 'Q'
    0x7C,0x66,0x66,0x7C,0x78,0x6C,0x66,0x00, // 'R'
    0x3C,0x66,0x60,0x3C,0x06,0x66,0x3C,0x00, // 'S'
    0x7E,0x18,0x18,0x18,0x18,0x18,0x18,0x00, // 'T'
    0x66,0x66,0x66,0x66,0x66,0x66,0x3C,0x00, // 'U'
    0x66,0x66,0x66,0x66,0x66,0x3C,0x18,0x00, // 'V'
    0x63,0x63,0x6B,0x7F,0x7F,0x77,0x66,0x00, // 'W'
    0x66,0x66,0x3C,0x18,0x3C,0x66,0x66,0x00, // 'X'
    0x66,0x66,0x66,0x3C,0x18,0x18,0x18,0x00, // 'Y'
    0x7E,0x06,0x0C,0x18,0x30,0x60,0x7E,0x00, // 'Z'
    0x62,0x66,0x0C,0x18,0x30,0x66,0x46,0x00, // '%'
    0x18,0x18,0x18,0x18,0x18,0x18,0x18,0x00, // '|'
    0x00,0x00,0x00,0x00,0x00,0x18,0x18,0x00, // '.'
    0x00,0x18,0x18,0x00,0x18,0x18,0x00,0x00, // ':'
    0x00,0x00,0x00,0x7E,0x00,0x00,0x00,0x00, // '-'
    0x02,0x06,0x0C,0x18,0x30,0x60,0x40,0x00, // '/'
];

fn vcall(com: *mut c_void, idx: usize) -> usize {
    unsafe { *(*(com as *const *const usize)).add(idx) }
}

fn d3d_compile(src: &str, entry: &str, target: &str) -> Option<Vec<u8>> {
    unsafe {
        let dll = LoadLibraryW(wide(r"d3dcompiler_47.dll").as_ptr());
        if dll.is_null() {
            return None;
        }
        // NOTE: D3DCompile takes LPCSTR (ANSI) for source name / entrypoint /
        // target. Passing UTF-16 wide strings makes the entrypoint read as ""
        // and compilation fails - a classic pitfall.
        type CompileFn = unsafe extern "system" fn(
            *const c_void, usize, *const u8, *const u8, *const u8,
            *const u8, *const u8, u32, u32, *mut *mut c_void, *mut *mut c_void,
        ) -> i32;
        let f: CompileFn = std::mem::transmute(GetProcAddress(dll, b"D3DCompile\0".as_ptr()));
        let mut blob: *mut c_void = ptr::null_mut();
        let mut err_blob: *mut c_void = ptr::null_mut();
        let mut src_name = entry.as_bytes().to_vec();
        src_name.push(0);
        let mut entry_b = entry.as_bytes().to_vec();
        entry_b.push(0);
        let mut target_b = target.as_bytes().to_vec();
        target_b.push(0);
        let hr = f(
            src.as_ptr() as *const c_void, src.len(),
            b"overlay.hlsl\0".as_ptr(), ptr::null(), ptr::null(),
            entry_b.as_ptr(), target_b.as_ptr(), 0, 0,
            &mut blob, &mut err_blob,
        );
        if hr < 0 || blob.is_null() {
            // Log the compiler error if available
            if !err_blob.is_null() {
                let esize: u32 = std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> u32>(vcall(err_blob, 4))(err_blob);
                let eptr: *mut u8 = std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> *mut u8>(vcall(err_blob, 3))(err_blob);
                let msg = String::from_utf8_lossy(std::slice::from_raw_parts(eptr, esize as usize));
                dbg(&format!("d3d_compile error: {}", msg));
            }
            return None;
        }
        // ID3D10Blob vtable: GetBufferPointer=3, GetBufferSize=4, Release=2
        let size: u32 = std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> u32>(vcall(blob, 4))(blob);
        let ptr: *mut u8 = std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> *mut u8>(vcall(blob, 3))(blob);
        let data = std::slice::from_raw_parts(ptr, size as usize).to_vec();
        std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> u32>(vcall(blob, 2))(blob); // Release
        Some(data)
    }
}

const VS_SRC: &str = r"
struct VSIn { float2 pos : POSITION; float2 uv : TEXCOORD; };
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD; };
VSOut main(VSIn i) {
    VSOut o; o.pos = float4(i.pos, 0.0, 1.0); o.uv = i.uv; return o;
}
";

const PS_SRC: &str = r"
Texture2D tex : register(t0);
SamplerState samp : register(s0);
float4 main(float4 pos : SV_POSITION, float2 uv : TEXCOORD) : SV_Target {
    return tex.Sample(samp, uv);
}
";

unsafe fn init_render(device: *mut c_void, ctx: *mut c_void) -> bool {
    // --- texture ---
    dbg(&format!(
        "init: device={:#x} vt9={:#x} texsize={}",
        device as usize,
        vcall(device, DEV_CREATE_TEXTURE2D),
        std::mem::size_of::<D3D11_TEXTURE2D_DESC>()
    ));
    let tex_desc = D3D11_TEXTURE2D_DESC {
        width: TEX_W, height: TEX_H, mip_levels: 1, array_size: 1,
        format: DXGI_FORMAT_R8G8B8A8_UNORM,
        sample_desc: DXGI_SAMPLE_DESC { count: 1, quality: 0 },
        usage: D3D11_USAGE_DYNAMIC,
        bind_flags: D3D11_BIND_SHADER_RESOURCE,
        cpu_access: D3D11_CPU_ACCESS_WRITE,
        misc_flags: D3D11_RESOURCE_MISC_NONE,
    };
    let create_tex: CreateTex2DFn = std::mem::transmute(vcall(device, DEV_CREATE_TEXTURE2D));
    let hr_tex = create_tex(device, &tex_desc, ptr::null(), &mut R_TEX);
    dbg(&format!("init: create tex hr={:#x} tex={:#x}", hr_tex, R_TEX as usize));
    if hr_tex < 0 || R_TEX.is_null() {
        dbg("init: create tex FAILED");
        return false;
    }
    dbg("init: create srv");
    let create_srv: CreateSRVFn = std::mem::transmute(vcall(device, DEV_CREATE_SRV));
    if create_srv(device, R_TEX, ptr::null(), &mut R_SRV) < 0 || R_SRV.is_null() {
        dbg("init: create srv FAILED");
        return false;
    }

    // --- shaders ---
    dbg("init: compile vs");
    let vs_blob = match d3d_compile(VS_SRC, "main", "vs_4_0") { Some(b) => b, None => { dbg("init: vs compile FAILED"); return false } };
    dbg("init: compile ps");
    let ps_blob = match d3d_compile(PS_SRC, "main", "ps_4_0") { Some(b) => b, None => { dbg("init: ps compile FAILED"); return false } };
    let create_vs: CreateShaderFn = std::mem::transmute(vcall(device, DEV_CREATE_VERTEX_SHADER));
    if create_vs(device, vs_blob.as_ptr(), vs_blob.len(), ptr::null_mut(), &mut R_VS) < 0 || R_VS.is_null() {
        return false;
    }
    let create_ps: CreateShaderFn = std::mem::transmute(vcall(device, DEV_CREATE_PIXEL_SHADER));
    if create_ps(device, ps_blob.as_ptr(), ps_blob.len(), ptr::null_mut(), &mut R_PS) < 0 || R_PS.is_null() {
        return false;
    }

    // --- input layout ---
    let elems = [
        D3D11_INPUT_ELEMENT_DESC {
            semantic_name: b"POSITION\0".as_ptr(), semantic_index: 0,
            format: 0x11, // DXGI_FORMAT_R32G32_FLOAT
            input_slot: 0, aligned_byte_offset: 0,
            input_slot_class: 0, instance_data_step_rate: 0,
        },
        D3D11_INPUT_ELEMENT_DESC {
            semantic_name: b"TEXCOORD\0".as_ptr(), semantic_index: 0,
            format: 0x11,
            input_slot: 0, aligned_byte_offset: 8,
            input_slot_class: 0, instance_data_step_rate: 0,
        },
    ];
    let create_il: CreateInputLayoutFn = std::mem::transmute(vcall(device, DEV_CREATE_INPUT_LAYOUT));
    if create_il(device, elems.as_ptr(), 2, vs_blob.as_ptr(), vs_blob.len(), &mut R_IL) < 0 || R_IL.is_null() {
        return false;
    }

    // --- vertex buffer (2 triangles, 6 verts, dynamic) ---
    dbg("init: create vb");
    let vb_desc = D3D11_BUFFER_DESC {
        byte_width: 6 * std::mem::size_of::<Vertex>() as u32,
        usage: D3D11_USAGE_DYNAMIC,
        bind_flags: 0x1, // VERTEX_BUFFER
        cpu_access: D3D11_CPU_ACCESS_WRITE,
        misc_flags: 0,
        structure_byte_stride: 0,
    };
    let create_buf: CreateBufferFn = std::mem::transmute(vcall(device, DEV_CREATE_BUFFER));
    if create_buf(device, &vb_desc, ptr::null(), &mut R_VB) < 0 || R_VB.is_null() {
        return false;
    }

    // --- blend state (alpha) ---
    let rt_desc = D3D11_RENDER_TARGET_BLEND_DESC {
        blend_enable: 1,
        src_blend: D3D_BLEND_SRC_ALPHA,
        dest_blend: D3D_BLEND_INV_SRC_ALPHA,
        blend_op: D3D_BLEND_OP_ADD,
        src_blend_alpha: D3D_BLEND_ONE,
        dest_blend_alpha: D3D_BLEND_INV_SRC_ALPHA,
        blend_op_alpha: D3D_BLEND_OP_ADD,
        render_target_write_mask: 0xF,
    };
    let blend_desc = D3D11_BLEND_DESC {
        alpha_to_coverage_enable: 0,
        independent_blend_enable: 0,
        render_target: [rt_desc; 8],
    };
    let create_blend: CreateBlendFn = std::mem::transmute(vcall(device, DEV_CREATE_BLEND_STATE));
    if create_blend(device, &blend_desc, &mut R_BLEND) < 0 || R_BLEND.is_null() {
        return false;
    }

    // --- sampler (point) ---
    let sampler_desc = D3D11_SAMPLER_DESC {
        filter: 0, // MIN_MAG_MIP_POINT
        address_u: 1, address_v: 1, address_w: 1, // CLAMP
        mip_lod_bias: 0.0, max_anisotropy: 1, comparison_func: 0,
        border_color: [0.0; 4], min_lod: 0.0, max_lod: 0.0,
    };
    let create_sampler: CreateSamplerFn = std::mem::transmute(vcall(device, DEV_CREATE_SAMPLER_STATE));
    if create_sampler(device, &sampler_desc, &mut R_SAMPLER) < 0 || R_SAMPLER.is_null() {
        return false;
    }

    R_DEVICE = device;
    R_CTX = ctx;
    RENDER_OK = true;
    dbg("init_render: ok");
    true
}

/// Rasterize text into a 1024x64 RGBA bitmap using the 8x8 font (scaled 2x).
fn rasterize_text(text: &[u8], buf: &mut [u8]) -> usize {
    buf.fill(0);
    let mut col = 0u32;
    let step = CHAR_W * SCALE;
    for &c in text {
        if c == 0 {
            break;
        }
        let idx = FONT_CHARS.iter().position(|&f| f == c);
        let glyph = match idx {
            Some(i) => &FONT_DATA[i * 8..(i + 1) * 8],
            None => {
                col += step; // unknown char -> space
                continue;
            }
        };
        if (col + step) > TEX_W {
            break;
        }
        for row in 0..CHAR_H {
            let bits = glyph[row as usize];
            for b in 0..CHAR_W {
                if bits & (1 << (7 - b)) != 0 {
                    for dy in 0..SCALE {
                        for dx in 0..SCALE {
                            let x = col + b * SCALE + dx;
                            let y = row * SCALE + dy;
                            let i = ((y * TEX_W + x) * 4) as usize;
                            buf[i] = 0;      // B (black text for white test bg)
                            buf[i + 1] = 0;  // G
                            buf[i + 2] = 0;  // R
                            buf[i + 3] = 255;  // A
                        }
                    }
                }
            }
        }
        col += step;
    }
    col as usize
}

unsafe fn render_frame(swapchain: *mut c_void) {
    if !RENDER_OK {
        return;
    }
    dbg("render_frame: start");
    let txt = read_shared_memory();
    let text = String::from_utf8_lossy(txt);
    let text = text.trim();
    dbg("render_frame: shm read ok");
    dbg(&format!("render_frame: swapchain={:#x}", swapchain as usize));

    // Get back buffer & viewport size from the game's swapchain
    dbg("render_frame: SC_GET_BUFFER ptr ready");
    let get_buffer: unsafe extern "system" fn(*mut c_void, u32, *const c_void, *mut *mut c_void) -> i32 =
        std::mem::transmute(vcall(swapchain, SC_GET_BUFFER));
    dbg("render_frame: get_buffer fn ready");
    if R_BACKBUF.is_null() || R_BACK_W == 0 {
        dbg("render_frame: fetching backbuf first time");
        let mut bb: *mut c_void = ptr::null_mut();
        // GetBuffer with IUnknown always succeeds; then QI to ID3D11Texture2D
        // (the typed IID sometimes returns E_NOINTERFACE from GetBuffer).
        // IID_IUnknown: 00000000-0000-0000-c000-000000000046
        let iid_unknown: [u8; 16] = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                     0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46];
        // IID_ID3D11Texture2D: 037e866e-f56d-4357-a8af-9dabbe6e250e
        let iid_tex2d: [u8; 16] = [0x6e, 0x86, 0x7e, 0x03, 0x6d, 0xf5, 0x57, 0x43,
                                   0xa8, 0xaf, 0x9d, 0xab, 0xbe, 0x6e, 0x25, 0x0e];
        let hr_gb = get_buffer(swapchain, 0, iid_unknown.as_ptr() as *const c_void, &mut bb);
        dbg(&format!("render_frame: GetBuffer hr={:#x} bb={:#x}", hr_gb, bb as usize));
        if hr_gb < 0 || bb.is_null() {
            return;
        }
        // QI to ID3D11Texture2D (needed by CreateRenderTargetView). If it
        // fails, bail out silently — the overlay just skips this frame.
        type QIFn = unsafe extern "system" fn(*mut c_void, *const u8, *mut *mut c_void) -> i32;
        let qi: QIFn = std::mem::transmute(vcall(bb, 0));
        let mut tex: *mut c_void = ptr::null_mut();
        let hr_qi = qi(bb, iid_tex2d.as_ptr(), &mut tex);
        dbg(&format!("render_frame: QI(Tex2D) hr={:#x} tex={:#x}", hr_qi, tex as usize));
        if hr_qi != 0 || tex.is_null() {
            return;
        }
        bb = tex;
        R_BACKBUF = bb;
        dbg("render_frame: GetDesc");
        let get_desc: unsafe extern "system" fn(*mut c_void, *mut DXGI_SWAP_CHAIN_DESC) -> i32 =
            std::mem::transmute(vcall(swapchain, SC_GET_DESC));
        let mut d = std::mem::zeroed::<DXGI_SWAP_CHAIN_DESC>();
        let hr_gd = get_desc(swapchain, &mut d);
        dbg(&format!("render_frame: GetDesc hr={:#x} w={} h={}", hr_gd, d.buffer_desc.width, d.buffer_desc.height));
        if hr_gd >= 0 {
            R_BACK_W = d.buffer_desc.width;
            R_BACK_H = d.buffer_desc.height;
        }
        dbg("render_frame: CreateRTV");
        let create_rtv: CreateRTVFn = std::mem::transmute(vcall(R_DEVICE, DEV_CREATE_RTV));
        let hr_rtv = create_rtv(R_DEVICE, R_BACKBUF, ptr::null(), &mut R_RTV);
        dbg(&format!("render_frame: CreateRTV hr={:#x} rtv={:#x}", hr_rtv, R_RTV as usize));
        if hr_rtv < 0 {
            R_RTV = ptr::null_mut();
        }
    }
    if R_RTV.is_null() || R_BACK_W == 0 || R_BACK_H == 0 {
        dbg(&format!("render_frame: early return, RTV={} W={} H={}", R_RTV.is_null(), R_BACK_W, R_BACK_H));
        return;
    }
    dbg("render_frame: about to rasterize");

    // Rasterize text into the 1024x64 bitmap
    let mut bitmap = [0u8; (TEX_W * TEX_H * 4) as usize];
    let used = rasterize_text(text.as_bytes(), &mut bitmap);
    if used == 0 {
        return;
    }

    // Upload texture
    dbg("render_frame: upload tex");
    let update: unsafe extern "system" fn(*mut c_void, *mut c_void, u32, *const c_void, *const c_void, usize, usize) =
        std::mem::transmute(vcall(R_CTX, CTX_UPDATE_SUBRESOURCE));
    update(R_CTX, R_TEX, 0, ptr::null(), bitmap.as_ptr() as *const c_void, (TEX_W * 4) as usize, 0);

    // Vertex data (NDC), quad covering (0,0)-(TEX_W*2, TEX_H*2) at top-left of backbuffer
    let w = R_BACK_W as f32;
    let h = R_BACK_H as f32;
    let x0 = 0f32;
    let y0 = 0f32;
    let x1 = (TEX_W * SCALE) as f32; // 2048 px wide text
    let y1 = (TEX_H * SCALE) as f32; // 128 px tall
    let nx = |x: f32| x / w * 2.0 - 1.0;
    let ny = |y: f32| 1.0 - y / h * 2.0;
    let verts = [
        Vertex { pos: [nx(x0), ny(y0)], uv: [0.0, 0.0] },
        Vertex { pos: [nx(x1), ny(y0)], uv: [1.0, 0.0] },
        Vertex { pos: [nx(x0), ny(y1)], uv: [0.0, 1.0] },
        Vertex { pos: [nx(x1), ny(y0)], uv: [1.0, 0.0] },
        Vertex { pos: [nx(x1), ny(y1)], uv: [1.0, 1.0] },
        Vertex { pos: [nx(x0), ny(y1)], uv: [0.0, 1.0] },
    ];

    // Update vertex buffer (map or UpdateSubresource on dynamic buffer)
    update(R_CTX, R_VB, 0, ptr::null(), verts.as_ptr() as *const c_void,
           (verts.len() * std::mem::size_of::<Vertex>()) as usize, 0);

    // Bind pipeline
    let vp = D3D11_VIEWPORT {
        top_left_x: 0.0, top_left_y: 0.0,
        width: w, height: h, min_depth: 0.0, max_depth: 1.0,
    };
    let rs_viewports: unsafe extern "system" fn(*mut c_void, u32, *const D3D11_VIEWPORT) = std::mem::transmute(vcall(R_CTX, CTX_RSSET_VIEWPORTS));
    rs_viewports(R_CTX, 1, &vp);
    let om_rt: unsafe extern "system" fn(*mut c_void, u32, *mut *mut c_void, *mut c_void) = std::mem::transmute(vcall(R_CTX, CTX_OMSET_RENDER_TARGETS));
    let mut rtv = R_RTV;
    om_rt(R_CTX, 1, &mut rtv, ptr::null_mut());
    let ia_il: unsafe extern "system" fn(*mut c_void, *mut c_void) = std::mem::transmute(vcall(R_CTX, CTX_IASET_INPUT_LAYOUT));
    ia_il(R_CTX, R_IL);
    let ia_vb: unsafe extern "system" fn(*mut c_void, u32, *mut *mut c_void, *const u32, *const u32) = std::mem::transmute(vcall(R_CTX, CTX_IASET_VERTEX_BUFFERS));
    let mut vb = R_VB;
    let stride = std::mem::size_of::<Vertex>() as u32;
    let offset = 0u32;
    ia_vb(R_CTX, 0, &mut vb, &stride, &offset);
    let ia_top: unsafe extern "system" fn(*mut c_void, u32) = std::mem::transmute(vcall(R_CTX, CTX_IASET_PRIMITIVE_TOPOLOGY));
    ia_top(R_CTX, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    let vs_set: unsafe extern "system" fn(*mut c_void, *mut c_void, *mut c_void, u32) = std::mem::transmute(vcall(R_CTX, CTX_VSSET_SHADER));
    vs_set(R_CTX, R_VS, ptr::null_mut(), 0);
    let ps_set: unsafe extern "system" fn(*mut c_void, *mut c_void, *mut c_void, u32) = std::mem::transmute(vcall(R_CTX, CTX_PSSET_SHADER));
    ps_set(R_CTX, R_PS, ptr::null_mut(), 0);
    let ps_srv: unsafe extern "system" fn(*mut c_void, u32, *mut *mut c_void) = std::mem::transmute(vcall(R_CTX, CTX_PSSET_SHADER_RESOURCES));
    let mut srv = R_SRV;
    ps_srv(R_CTX, 0, &mut srv);
    let ps_samp: unsafe extern "system" fn(*mut c_void, u32, *mut *mut c_void) = std::mem::transmute(vcall(R_CTX, CTX_PSSET_SAMPLERS));
    let mut samp = R_SAMPLER;
    ps_samp(R_CTX, 0, &mut samp);
    let om_blend: unsafe extern "system" fn(*mut c_void, *mut c_void, *const f32, u32) = std::mem::transmute(vcall(R_CTX, CTX_OMSET_BLEND_STATE));
    om_blend(R_CTX, R_BLEND, ptr::null(), 0xffffffff);

    let draw: unsafe extern "system" fn(*mut c_void, u32, u32) = std::mem::transmute(vcall(R_CTX, CTX_DRAW));
    dbg("render_frame: draw");
    draw(R_CTX, 6, 0);
    dbg("render_frame: done");
}

static mut FRAME_COUNT: u32 = 0;

fn dbg(msg: &str) {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(r"C:\Users\YeTom\WorkBuddy\2026-08-05-22-32-13\overlay_dbg.txt")
    {
        let _ = writeln!(f, "{}", msg);
    }
}

unsafe extern "system" fn hooked_present(swapchain: *mut c_void, sync: u32, flags: u32) -> i32 {
    FRAME_COUNT += 1;
    // Lazy-init rendering from the game's REAL swapchain on first Present.
    if !RENDER_OK {
        if init_render_from_swapchain(swapchain) {
            dbg("hooked_present: render initialized");
            let _ = std::fs::write(
                r"C:\Users\YeTom\WorkBuddy\2026-08-05-22-32-13\overlay_hook.txt",
                format!("hook=true render=true pid={}", std::process::id()),
            );
        }
    }
    // Draw overlay (no-op until init succeeds).
    if RENDER_OK {
        render_frame(swapchain);
    }
    let orig: PresentFn = std::mem::transmute(ORIG_PRESENT);
    orig(swapchain, sync, flags)
}

// IID_IDXGIDevice: 54ec77fa-1377-44e6-8b08-6c0d34fd3c9e (LE bytes)
const IID_IDXGIDEVICE: [u8; 16] = [0xfa, 0x77, 0xec, 0x54, 0x77, 0x13, 0xe6, 0x44,
                                   0x8b, 0x08, 0x6c, 0x0d, 0x34, 0xfd, 0x3c, 0x9e];
// IID_ID3D11Device: db6f6ddb-ac77-4e88-8253-819df9bbf140 (LE bytes)
const IID_ID3D11DEVICE: [u8; 16] = [0xdb, 0x6d, 0x6f, 0xdb, 0x77, 0xac, 0x88, 0x4e,
                                    0x82, 0x53, 0x81, 0x9d, 0xf9, 0xbb, 0xf1, 0x40];

/// Lazy-initialize rendering from the game's real swapchain. On the first
/// Present we resolve the game's ID3D11Device via swapchain->GetDevice and
/// build every overlay resource on it (no cross-device resource sharing).
unsafe fn init_render_from_swapchain(swapchain: *mut c_void) -> bool {
    dbg("init_from_sc: entered");
    // swapchain->GetDevice(riid, &out). DXGI vtable has no DeviceChild layer:
    // GetDevice is deterministically slot 7 (Present=8, GetBuffer=9 verified).
    // Some drivers return E_NOINTERFACE for the typed IID but accept
    // IID_IUnknown, so try that first, then the typed IID.
    type GetDevFn = unsafe extern "system" fn(*mut c_void, *const u8, *mut *mut c_void) -> i32;
    let g: GetDevFn = std::mem::transmute(vcall(swapchain, 7));
    // IID_IUnknown: 00000000-0000-0000-c000-000000000046 (LE bytes)
    let iid_unknown: [u8; 16] = [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46];
    let mut idxgi: *mut c_void = ptr::null_mut();
    let mut hr = g(swapchain, iid_unknown.as_ptr(), &mut idxgi);
    dbg(&format!("init_from_sc: GetDevice@7 (IUnknown) hr={:#x}", hr));
    if hr != 0 || idxgi.is_null() {
        idxgi = ptr::null_mut();
        hr = g(swapchain, IID_IDXGIDEVICE.as_ptr(), &mut idxgi);
        dbg(&format!("init_from_sc: GetDevice@7 (IDXGIDevice) hr={:#x}", hr));
    }
    if hr != 0 || idxgi.is_null() {
        return false;
    }

    // idxgi->QueryInterface(IID_ID3D11Device, &device)
    type QIFn = unsafe extern "system" fn(*mut c_void, *const u8, *mut *mut c_void) -> i32;
    let qi: QIFn = std::mem::transmute(vcall(idxgi, 0));
    let mut device: *mut c_void = ptr::null_mut();
    let hr_qi = qi(idxgi, IID_ID3D11DEVICE.as_ptr(), &mut device);
    dbg(&format!("init_from_sc: QI(ID3D11Device) hr={:#x} dev={:#x}", hr_qi, device as usize));
    if hr_qi != 0 || device.is_null() {
        return false;
    }

    // device->GetImmediateContext(&ctx). This d3d11's device vtable is
    // official - 4, so GetImmediateContext (official 32) is at 28.
    dbg("init_from_sc: dump device vt[22..36]");
    {
        let vt = *(device as *const *const usize);
        for chunk in [22usize, 26, 30, 34] {
            let mut s = String::new();
            for i in chunk..chunk + 4 {
                let p = vt.add(i);
                s += &format!(" [{i}]={:#x}", *p);
            }
            dbg(&s);
        }
    }
    type GetCtxFn = unsafe extern "system" fn(*mut c_void, *mut *mut c_void) -> i32;
    let mut ctx: *mut c_void = ptr::null_mut();
    let mut ctx_ok = false;
    // Empirical scan found GetImmediateContext at slot 40 on this system's
    // d3d11 (writes a valid heap pointer). Keep neighbours as fallback.
    for idx in &[40usize, 39, 41, 28, 32] {
        dbg(&format!("init_from_sc: calling GetImmediateContext@{idx}"));
        let g: GetCtxFn = std::mem::transmute(vcall(device, *idx));
        let hr = g(device, &mut ctx);
        dbg(&format!("init_from_sc: GetImmediateContext@{idx} hr={:#x} ctx={:#x}", hr, ctx as usize));
        // Accept a plausible heap/COM pointer (high address), not tiny flags.
        let v = ctx as usize;
        if v > 0x10000 && (v >> 48) == 0 && v >> 32 != 0 {
            ctx_ok = true;
            break;
        }
        ctx = ptr::null_mut();
    }
    if !ctx_ok {
        return false;
    }

    let render_ok = init_render(device, ctx);
    dbg(&format!("init_from_sc: init_render={render_ok}"));
    render_ok
}

// ---------------------------------------------------------------------------
// Worker thread
// ---------------------------------------------------------------------------
extern "system" fn worker(_arg: *mut c_void) -> u32 {
    // Standard imgui-style D3D11 hook: create a dummy swapchain in THIS
    // process to obtain the IDXGISwapChain vtable, then patch the Present
    // slot. Every swapchain created afterwards by the game shares the same
    // d3d11.dll vtable, so its Present calls route through our hook. This
    // needs no cooperation from the game and no shared memory.
    dbg("worker: creating dummy swapchain");
    unsafe {
        match create_dummy_swapchain() {
            Some((dummy_sc, _, _)) => {
                let ok = hook_present(dummy_sc);
                let _ = std::fs::write(
                    r"C:\Users\YeTom\WorkBuddy\2026-08-05-22-32-13\overlay_hook.txt",
                    format!("hook={} pid={} (dummy)", ok, std::process::id()),
                );
                dbg("worker: hook done (dummy)");
                // Keep the dummy swapchain alive — its vtable backs the hook.
                loop {
                    std::thread::sleep(std::time::Duration::from_secs(1));
                }
            }
            None => {
                let _ = std::fs::write(
                    r"C:\Users\YeTom\WorkBuddy\2026-08-05-22-32-13\overlay_hook.txt",
                    "d3d11 create failed",
                );
                1
            }
        }
    }
}
