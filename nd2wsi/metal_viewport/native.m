// macOS-only, isolated Agent viewport. Never attach this controller to a User
// window. Production draws raw channels directly to a drawable; the explicitly
// named offscreen validation ABI is the only CPU pixel readback in this file.
#import <AppKit/AppKit.h>
#import <MetalKit/MetalKit.h>
#import <simd/simd.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/resource.h>
#include <mach/mach.h>
#include <errno.h>

#define API __attribute__((visibility("default")))
#define MAX_CHANNELS 32
#define MEMORY_BUDGET (256ull * 1024 * 1024)
#define MAX_REQUESTS 4
#define MAX_BACKPRESSURE_RETRIES 5

static BOOL VPNumber(id value, double low, double high) {
    return [value isKindOfClass:NSNumber.class] &&
        CFGetTypeID((__bridge CFTypeRef)value)!=CFBooleanGetTypeID() &&
        isfinite([value doubleValue]) && [value doubleValue]>=low && [value doubleValue]<=high;
}
static BOOL VPArray(id value, NSUInteger count) {
    return [value isKindOfClass:NSArray.class] && [value count]==count;
}
static BOOL VPInteger(id value, double low, double high) {
    return VPNumber(value,low,high) && floor([value doubleValue])==[value doubleValue];
}
static BOOL VPBlocksReplayInput(BOOL replaying, BOOL agent, BOOL ownWindow) {
    return replaying && agent && ownWindow;
}
API int nd2wsi_viewport_replay_input_scope(int replaying, int agent, int own_window) {
    return VPBlocksReplayInput(replaying!=0,agent!=0,own_window!=0)?1:0;
}
static BOOL VPMetadataValid(id meta) {
    if(![meta isKindOfClass:NSDictionary.class] || ![meta[@"dtype"] isEqual:@"uint16"] ||
       !VPInteger(meta[@"width"],1,INT32_MAX) || !VPInteger(meta[@"height"],1,INT32_MAX) ||
       !VPInteger(meta[@"tile_size"],1,512) || ![meta[@"levels"] isKindOfClass:NSArray.class] ||
       ![meta[@"levels"] count] || ![meta[@"channels"] isKindOfClass:NSArray.class] ||
       ![meta[@"channels"] count] || [meta[@"channels"] count]>8)return NO;
    NSMutableSet *paths=[NSMutableSet new]; double previous=0, width=[meta[@"width"] doubleValue],height=[meta[@"height"] doubleValue];
    for(id level in meta[@"levels"]) {
        if(![level isKindOfClass:NSDictionary.class] || !VPInteger(level[@"width"],1,width) ||
           !VPInteger(level[@"height"],1,height) || !VPInteger(level[@"downsample"],1,INT32_MAX) ||
           [level[@"downsample"] doubleValue]<=previous || ![level[@"path"] isKindOfClass:NSString.class] ||
           ![level[@"path"] length] || [level[@"path"] length]>9 || [paths containsObject:level[@"path"]] ||
           [level[@"path"] rangeOfCharacterFromSet:NSCharacterSet.decimalDigitCharacterSet.invertedSet].location!=NSNotFound)return NO;
        if(!previous && ([level[@"downsample"] doubleValue]!=1 || [level[@"width"] doubleValue]!=width || [level[@"height"] doubleValue]!=height))return NO;
        previous=[level[@"downsample"] doubleValue];width=[level[@"width"] doubleValue];height=[level[@"height"] doubleValue];[paths addObject:level[@"path"]];
    }
    for(id channel in meta[@"channels"]) {
        if(![channel isKindOfClass:NSDictionary.class] || ![channel[@"label"] isKindOfClass:NSString.class] ||
           !VPArray(channel[@"window"],2) || !VPNumber(channel[@"window"][0],0,65535) ||
           !VPNumber(channel[@"window"][1],0,65536) || [channel[@"window"][1] doubleValue]<=[channel[@"window"][0] doubleValue] ||
           !VPArray(channel[@"color"],3))return NO;
        for(id value in channel[@"color"])if(!VPNumber(value,0,255))return NO;
    } return YES;
}
static BOOL VPStateValid(id state, double width, double height, NSUInteger channels) {
    if(![state isKindOfClass:NSDictionary.class] || !VPNumber(state[@"version"],1,1) ||
       !VPArray(state[@"source_dimensions"],2) || !VPNumber(state[@"source_dimensions"][0],width,width) ||
       !VPNumber(state[@"source_dimensions"][1],height,height) || !VPArray(state[@"center"],2) ||
       !VPNumber(state[@"center"][0],0,width) || !VPNumber(state[@"center"][1],0,height) ||
       !VPNumber(state[@"zoom"],1e-12,32) || !VPArray(state[@"channels"],channels))return NO;
    for(id channel in state[@"channels"]) {
        if(![channel isKindOfClass:NSDictionary.class] || !VPArray(channel[@"window"],2) ||
           !VPNumber(channel[@"window"][0],0,65535) || !VPNumber(channel[@"window"][1],0,65536) ||
           [channel[@"window"][1] doubleValue]<=[channel[@"window"][0] doubleValue] ||
           !VPNumber(channel[@"gamma"],.1,10) || !VPArray(channel[@"color"],3) ||
           ![channel[@"visible"] isKindOfClass:NSNumber.class] ||
           CFGetTypeID((__bridge CFTypeRef)channel[@"visible"])!=CFBooleanGetTypeID())return NO;
        for(id value in channel[@"color"])if(!VPNumber(value,0,255))return NO;
    }return YES;
}
// Validation ABI: pure metadata checks, no window, file access or pixel readback.
API int nd2wsi_viewport_validate_contract(const char *metadata_json, const char *state_json) {
    @autoreleasepool {
        if(!metadata_json)return 1;
        id metadata=[NSJSONSerialization JSONObjectWithData:[[NSString stringWithUTF8String:metadata_json] dataUsingEncoding:NSUTF8StringEncoding] options:NSJSONReadingFragmentsAllowed error:nil];
        if(!VPMetadataValid(metadata))return 1;
        if(!state_json)return 0;
        id state=[NSJSONSerialization JSONObjectWithData:[[NSString stringWithUTF8String:state_json] dataUsingEncoding:NSUTF8StringEncoding] options:NSJSONReadingFragmentsAllowed error:nil];
        return VPStateValid(state,[metadata[@"width"] doubleValue],[metadata[@"height"] doubleValue],[metadata[@"channels"] count])?0:2;
    }
}

typedef struct { vector_float4 positionUV; } VPVertex;
typedef struct {
    vector_uint4 dimensions;
    vector_float4 windowGamma[MAX_CHANNELS];
    vector_float4 colors[MAX_CHANNELS];
} VPUniforms;

static NSString *const VPShader = @
"#include <metal_stdlib>\n"
"using namespace metal;\n"
"struct V { float4 positionUV; };\n"
"struct O { float4 position [[position]]; float2 uv; };\n"
"struct U { uint4 dimensions; float4 windowGamma[32]; float4 colors[32]; };\n"
"vertex O vp_vertex(uint id [[vertex_id]], constant V *v [[buffer(0)]]) {\n"
" O o; o.position=float4(v[id].positionUV.xy,0,1); o.uv=v[id].positionUV.zw; return o; }\n"
"fragment float4 vp_fragment(O in [[stage_in]], device const ushort *raw [[buffer(0)]],\n"
" constant U &u [[buffer(1)]]) {\n"
" uint x=min(uint(max(in.uv.x,0.0f)),u.dimensions.x-1);\n"
" uint y=min(uint(max(in.uv.y,0.0f)),u.dimensions.y-1); float3 rgb=0;\n"
" for(uint c=0;c<u.dimensions.z;c++){ float4 p=u.windowGamma[c]; if(p.w<0.5f) continue;\n"
" float rawValue=float(raw[c*u.dimensions.x*u.dimensions.y+y*u.dimensions.x+x]);\n"
" float v=clamp((rawValue-p.x)/max(p.y-p.x,1e-6f),0.0f,1.0f);\n"
" if(p.z!=1.0f) v=pow(v,1.0f/p.z); rgb+=v*u.colors[c].xyz; }\n"
" return float4(floor(clamp(rgb,0.0f,1.0f)*255.0f)/255.0f,1.0f); }\n";

static void VPError(char *out, size_t capacity, NSString *value) {
    if (out && capacity) snprintf(out, capacity, "%s", value.UTF8String ?: "unknown error");
}

static NSDictionary *VPProcessMemory(void) {
    struct rusage usage={0}; getrusage(RUSAGE_SELF,&usage);
    task_vm_info_data_t vm={0}; mach_msg_type_number_t count=TASK_VM_INFO_COUNT;
    kern_return_t status=task_info(mach_task_self(),TASK_VM_INFO,(task_info_t)&vm,&count);
    return @{@"peak_rss_bytes":@(usage.ru_maxrss),
             @"resident_bytes":status==KERN_SUCCESS?@(vm.resident_size):[NSNull null],
             @"physical_footprint_bytes":status==KERN_SUCCESS?@(vm.phys_footprint):[NSNull null]};
}

static BOOL VPViewportDimension(const char *name, NSInteger fallback,
        NSInteger minimum, NSInteger maximum, NSInteger *value, NSError **error) {
    const char *setting=getenv(name);
    if(!setting){*value=fallback;return YES;}
    char *end=NULL;errno=0;long parsed=strtol(setting,&end,10);
    if(errno || !setting[0] || end==setting || *end || parsed<minimum || parsed>maximum) {
        if(error)*error=[NSError errorWithDomain:@"nd2wsi.MetalViewport" code:EINVAL
            userInfo:@{NSLocalizedDescriptionKey:[NSString stringWithFormat:@"%s must be an integer from %ld to %ld points",name,(long)minimum,(long)maximum]}];
        return NO;
    }
    *value=(NSInteger)parsed;return YES;
}

static id<MTLRenderPipelineState> VPPipeline(id<MTLDevice> device, NSError **error) {
    MTLCompileOptions *options = [MTLCompileOptions new];
    options.fastMathEnabled = NO;
    id<MTLLibrary> library = [device newLibraryWithSource:VPShader options:options error:error];
    if (!library) return nil;
    MTLRenderPipelineDescriptor *desc = [MTLRenderPipelineDescriptor new];
    desc.label = @"ND2 raw channels fused window gamma LUT composite";
    desc.vertexFunction = [library newFunctionWithName:@"vp_vertex"];
    desc.fragmentFunction = [library newFunctionWithName:@"vp_fragment"];
    desc.colorAttachments[0].pixelFormat = MTLPixelFormatBGRA8Unorm;
    desc.colorAttachments[0].blendingEnabled = NO;
    return [device newRenderPipelineStateWithDescriptor:desc error:error];
}

// Validation-only ABI. This deliberately reads an offscreen target, never a
// displayed drawable; its transfers must not be included in production metrics.
API int nd2wsi_viewport_composite(const uint16_t *raw, uint32_t channels,
        uint32_t width, uint32_t height, const float *windows, const float *gammas,
        const float *colors, const uint8_t *visible, uint8_t *rgb,
        char *error, size_t error_capacity) {
    @autoreleasepool {
        if (!raw || !windows || !gammas || !colors || !visible || !rgb ||
            !width || !height || !channels || channels > MAX_CHANNELS ||
            width > 16384 || height > 16384 ||
            (uint64_t)width * height * channels * 2 > MEMORY_BUDGET) {
            VPError(error, error_capacity, @"Invalid or over-budget validation input"); return 1;
        }
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device || !device.hasUnifiedMemory) {
            VPError(error, error_capacity, @"Apple silicon unified-memory Metal device required"); return 2;
        }
        NSError *problem = nil;
        id<MTLRenderPipelineState> pipeline = VPPipeline(device, &problem);
        if (!pipeline) { VPError(error, error_capacity, problem.localizedDescription); return 3; }
        size_t inputBytes = (size_t)channels * width * height * 2;
        id<MTLBuffer> buffer = [device newBufferWithBytes:raw length:inputBytes options:MTLResourceStorageModeShared];
        MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:width height:height mipmapped:NO];
        td.storageMode = MTLStorageModeShared;
        td.usage = MTLTextureUsageRenderTarget;
        id<MTLTexture> target = [device newTextureWithDescriptor:td];
        if (!buffer || !target) { VPError(error, error_capacity, @"Metal allocation failed"); return 4; }
        VPUniforms uniform = {0};
        uniform.dimensions = (vector_uint4){width, height, channels, 0};
        for (uint32_t c = 0; c < channels; ++c) {
            if (!isfinite(windows[c*2]) || !isfinite(windows[c*2+1]) ||
                !isfinite(gammas[c]) || gammas[c] < 0.1f || gammas[c] > 10.f) {
                VPError(error, error_capacity, @"Invalid display parameters"); return 1;
            }
            uniform.windowGamma[c] = (vector_float4){windows[c*2], windows[c*2+1], gammas[c], visible[c] ? 1.f : 0.f};
            uniform.colors[c] = (vector_float4){colors[c*3]/255.f, colors[c*3+1]/255.f, colors[c*3+2]/255.f, 0};
        }
        VPVertex vertices[6] = {{{-1,1,0,0}},{{-1,-1,0,height}},{{1,-1,width,height}},
                                {{-1,1,0,0}},{{1,-1,width,height}},{{1,1,width,0}}};
        MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
        pass.colorAttachments[0].texture = target;
        pass.colorAttachments[0].loadAction = MTLLoadActionClear;
        pass.colorAttachments[0].storeAction = MTLStoreActionStore;
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLCommandBuffer> command = [queue commandBuffer];
        id<MTLRenderCommandEncoder> encoder = [command renderCommandEncoderWithDescriptor:pass];
        [encoder setRenderPipelineState:pipeline];
        [encoder setVertexBytes:vertices length:sizeof(vertices) atIndex:0];
        [encoder setFragmentBuffer:buffer offset:0 atIndex:0];
        [encoder setFragmentBytes:&uniform length:sizeof(uniform) atIndex:1];
        [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:6];
        [encoder endEncoding]; [command commit]; [command waitUntilCompleted];
        if (command.status != MTLCommandBufferStatusCompleted) {
            VPError(error, error_capacity, command.error.localizedDescription); return 5;
        }
        uint8_t *bgra = malloc((size_t)width * height * 4);
        if (!bgra) { VPError(error, error_capacity, @"Readback allocation failed"); return 4; }
        [target getBytes:bgra bytesPerRow:(size_t)width*4 fromRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0];
        for (size_t i=0; i<(size_t)width*height; ++i) {
            rgb[i*3]=bgra[i*4+2]; rgb[i*3+1]=bgra[i*4+1]; rgb[i*3+2]=bgra[i*4];
        }
        free(bgra); return 0;
    }
}

@class VPController;
@interface VPTile : NSObject
@property NSString *key;
@property id<MTLBuffer> buffer;
@property NSUInteger width, height, tx, ty, level, gpuUsers;
@property uint64_t bytes;
@property double lastUse;
@end
@implementation VPTile
@end

@interface VPRequest : NSObject
@property NSURLSessionDataTask *task;
@property uint64_t bytes, generation;
@end
@implementation VPRequest
@end

// A short, non-flipped NSScrollView document sits at the bottom of its clip
// view. Flipping the controls document makes the first control top-aligned
// without pinning it (and thereby disabling normal scrolling).
@interface VPControls : NSStackView
@end
@implementation VPControls
- (BOOL)isFlipped { return YES; }
@end

@interface VPView : MTKView
@property(weak) VPController *controller;
@property NSPoint dragStart;
@end

@interface VPController : NSObject <MTKViewDelegate, NSWindowDelegate, NSApplicationDelegate>
@property NSString *baseURL, *reportPath;
@property NSDictionary *session, *metadata;
@property NSWindow *window;
@property VPView *view;
@property NSStackView *controls;
@property NSTextField *status;
@property id<MTLDevice> device;
@property id<MTLRenderPipelineState> pipeline;
@property id<MTLCommandQueue> queue;
@property NSURLSession *network;
@property NSMutableDictionary<NSString *, VPTile *> *tiles;
@property NSMutableDictionary<NSString *, VPRequest *> *requests;
@property NSMutableSet<VPRequest *> *retiringRequests;
@property NSMutableSet<NSString *> *failedKeys;
@property NSMutableDictionary<NSString *, NSNumber *> *retryCounts, *retryDeadlines;
@property NSMutableArray<NSDictionary *> *wanted;
@property NSMutableArray<NSSlider *> *lows, *highs, *gammas;
@property NSMutableArray<NSButton *> *checks;
@property NSMutableArray<NSPopUpButton *> *colorMenus;
@property NSMutableArray<NSMutableDictionary *> *frames;
@property NSMutableArray<NSDictionary *> *actions, *errors;
@property NSArray<NSDictionary *> *levels;
@property NSDictionary *backendMetrics;
@property NSMutableDictionary *outcome;
@property NSURLSessionDataTask *metadataTask;
@property id replayInputMonitor;
@property NSMutableArray<NSArray *> *initialColors;
@property NSMutableDictionary *capture;
@property double centerX, centerY, zoom, imageWidth, imageHeight, lastInput, previousPresentation, backpressureUntil;
@property NSUInteger channels, tileSize, activeLevel, replayStep, activeCommands;
@property uint64_t generation, actionID, residentBytes, pendingBytes, peakBytes, peakFootprint, uploadedBytes, requestCount, cancelledRequests, backpressureResponses, backpressureRetries, exhaustedRetries, presentedCount, droppedCount, encodeCount;
@property BOOL ready, closed, capturePending, captureRunning, replaying, frameStreamed;
@property BOOL stopping, internalClose, finalized, firstPresented, faultUsed;
@property NSUInteger memoryFailures;
@property NSString *mode;
@property double replayWaitStart;
- (void)pan:(NSPoint)delta;
- (void)zoomAt:(NSPoint)point delta:(double)delta;
- (void)viewportChanged:(NSString *)action;
- (void)saveReport;
- (NSString *)actionToken;
- (void)finishKind:(NSString *)kind failure:(NSString *)failure reason:(NSString *)reason;
- (void)finishIfDrained;
- (NSDictionary *)viewState;
- (BOOL)fault:(NSString *)name;
- (VPUniforms)uniformsFor:(VPTile *)tile;
- (void)confirmFirstPresentation:(NSDictionary *)frame;
- (void)stopReplayInputMonitor;
@end

@implementation VPView
- (BOOL)acceptsFirstResponder { return YES; }
- (void)mouseDown:(NSEvent *)event { self.dragStart=[self convertPoint:event.locationInWindow fromView:nil]; }
- (void)mouseDragged:(NSEvent *)event {
    NSPoint point=[self convertPoint:event.locationInWindow fromView:nil];
    [self.controller pan:NSMakePoint(point.x-self.dragStart.x, point.y-self.dragStart.y)]; self.dragStart=point;
}
- (void)scrollWheel:(NSEvent *)event {
    NSPoint point=[self convertPoint:event.locationInWindow fromView:nil];
    [self.controller zoomAt:point delta:event.scrollingDeltaY * (event.hasPreciseScrollingDeltas ? 0.012 : 0.10)];
}
- (void)magnifyWithEvent:(NSEvent *)event {
    [self.controller zoomAt:[self convertPoint:event.locationInWindow fromView:nil] delta:event.magnification];
}
- (void)keyDown:(NSEvent *)event {
    if ([event.characters isEqualToString:@"+"] || [event.characters isEqualToString:@"="])
        [self.controller zoomAt:NSMakePoint(NSMidX(self.bounds),NSMidY(self.bounds)) delta:0.25];
    else if ([event.characters isEqualToString:@"-"])
        [self.controller zoomAt:NSMakePoint(NSMidX(self.bounds),NSMidY(self.bounds)) delta:-0.25];
    else [super keyDown:event];
}
@end

static NSTextField *VPLabel(NSString *value) {
    NSTextField *label=[NSTextField labelWithString:value];
    label.font=[NSFont systemFontOfSize:11];
    return label;
}

static NSDictionary *VPSummary(NSArray<NSNumber *> *values) {
    if (!values.count) return @{@"count":@0};
    NSArray *sorted=[values sortedArrayUsingSelector:@selector(compare:)];
    double (^percentile)(double)=^double(double p) {
        double index=p*(sorted.count-1); NSUInteger lo=(NSUInteger)floor(index), hi=(NSUInteger)ceil(index);
        return [sorted[lo] doubleValue]*(hi-index)+[sorted[hi] doubleValue]*(index-lo) + (lo==hi ? [sorted[lo] doubleValue] : 0);
    };
    return @{@"count":@(values.count),@"p50":@(percentile(.50)),@"p95":@(percentile(.95)),@"p99":@(percentile(.99))};
}

@implementation VPController
- (instancetype)init {
    if ((self=[super init])) {
        _tiles=[NSMutableDictionary new]; _requests=[NSMutableDictionary new]; _wanted=[NSMutableArray new];
        _retiringRequests=[NSMutableSet new]; _failedKeys=[NSMutableSet new];
        _retryCounts=[NSMutableDictionary new]; _retryDeadlines=[NSMutableDictionary new];
        _frames=[NSMutableArray new]; _actions=[NSMutableArray new]; _errors=[NSMutableArray new];
        _lows=[NSMutableArray new]; _highs=[NSMutableArray new]; _gammas=[NSMutableArray new];
        _checks=[NSMutableArray new]; _colorMenus=[NSMutableArray new];
        _initialColors=[NSMutableArray new];
        _capture=[@{@"requested":@NO,@"completed":@NO} mutableCopy]; _mode=@"interactive";
        NSURLSessionConfiguration *config=[NSURLSessionConfiguration ephemeralSessionConfiguration];
        config.HTTPMaximumConnectionsPerHost=MAX_REQUESTS;
        config.timeoutIntervalForRequest=15; config.timeoutIntervalForResource=30;
        config.requestCachePolicy=NSURLRequestReloadIgnoringLocalCacheData;
        _network=[NSURLSession sessionWithConfiguration:config];
    } return self;
}
- (NSURL *)URLFor:(NSString *)relative {
    return [NSURL URLWithString:relative relativeToURL:[NSURL URLWithString:self.baseURL]].absoluteURL;
}
- (void)recordError:(NSString *)reason {
    if (self.errors.count<100) [self.errors addObject:@{@"time":@(CACurrentMediaTime()),@"reason":reason?:@"unknown"}];
    self.status.stringValue=reason?:@"Unknown failure";
}
- (BOOL)fault:(NSString *)name {
    id diagnostic=self.session[@"diagnostics"];
    return [self.session[@"role"] isEqual:@"agent"] && [diagnostic isKindOfClass:NSDictionary.class] &&
        [diagnostic[@"enabled"] isEqual:@YES] && [diagnostic[@"fault"] isEqual:name];
}
- (void)confirmFirstPresentation:(NSDictionary *)frame {
    if(self.firstPresented || [frame[@"presented_time"] doubleValue]<=0 ||
       ![frame[@"tiles_drawn"] unsignedIntegerValue] || ![frame[@"gpu_status"] isEqual:@"completed"])return;
    self.firstPresented=YES;[self saveReport];
    id diagnostic=self.session[@"diagnostics"];
    if([self.session[@"role"] isEqual:@"agent"] && [diagnostic isKindOfClass:NSDictionary.class] &&
       [diagnostic[@"enabled"] isEqual:@YES] && [diagnostic[@"close_after_first_presented"] isEqual:@YES])
        dispatch_async(dispatch_get_main_queue(),^{[self finishKind:@"closed" failure:nil reason:nil];});
    else if([self.session[@"role"] isEqual:@"agent"] && [diagnostic isKindOfClass:NSDictionary.class] &&
       [diagnostic[@"enabled"] isEqual:@YES] && [diagnostic[@"handoff_after_first_presented"] isEqual:@YES])
        dispatch_async(dispatch_get_main_queue(),^{[self standardViewer:nil];});
}
- (void)finishKind:(NSString *)kind failure:(NSString *)failure reason:(NSString *)reason {
    NSAssert(NSThread.isMainThread,@"Lifecycle transitions are main-thread serialized");
    if(self.finalized)return;
    // A close already in progress wins over late metadata/GPU/network errors.
    if(self.stopping && ![kind isEqual:@"closed"])return;
    if(reason)[self recordError:reason];
    self.outcome=[@{@"kind":kind,@"first_presented":@(self.firstPresented)} mutableCopy];
    if(failure)self.outcome[@"failure_kind"]=failure;
    NSDictionary *state=[self viewState];if(state)self.outcome[@"view_state"]=state;
    self.stopping=YES;self.closed=YES;self.replaying=NO;self.capturePending=NO;
    [self stopReplayInputMonitor];
    self.view.paused=YES;self.view.delegate=nil;
    [self.metadataTask cancel];
    for(VPRequest *request in self.requests.allValues)[request.task cancel];
    for(VPRequest *request in self.retiringRequests)[request.task cancel];
    [self.network invalidateAndCancel];
    // Keep the event loop alive until all committed GPU work has released its
    // retained tiles. No waitUntilCompleted on the UI thread and no early pool reuse.
    if([self fault:@"close_race"] && ![kind isEqual:@"closed"])
        dispatch_async(dispatch_get_main_queue(),^{[self finishKind:@"closed" failure:nil reason:nil];});
    dispatch_async(dispatch_get_main_queue(),^{[self finishIfDrained];});
}
- (void)finishIfDrained {
    if(!self.stopping || self.finalized || self.activeCommands)return;
    self.finalized=YES;
    if(self.captureRunning){[MTLCaptureManager.sharedCaptureManager stopCapture];self.captureRunning=NO;}
    self.outcome[@"first_presented"]=@(self.firstPresented);
    [self saveReport];self.internalClose=YES;[self.window close];
    [NSApp stop:nil];
    [NSApp postEvent:[NSEvent otherEventWithType:NSEventTypeApplicationDefined location:NSZeroPoint modifierFlags:0 timestamp:0 windowNumber:0 context:nil subtype:0 data1:0 data2:0] atStart:NO];
}
- (void)launchWindowCommand:(NSString *)key {
    if(self.closed)return;
    NSArray *command=self.session[@"window_commands"][key];
    if(![command isKindOfClass:NSArray.class] || command.count<2 ||
       ![command[0] isKindOfClass:NSString.class] || ![command[0] hasPrefix:@"/"])return;
    for(id part in command)if(![part isKindOfClass:NSString.class])return;
    NSTask *task=[NSTask new]; task.executableURL=[NSURL fileURLWithPath:command[0]];
    task.arguments=[command subarrayWithRange:NSMakeRange(1,command.count-1)];
    NSMutableDictionary *environment=[NSProcessInfo.processInfo.environment mutableCopy];
    environment[@"PYINSTALLER_RESET_ENVIRONMENT"]=@"1";
    environment[@"ND2WSI_WINDOW_CHILD"]=@"1";
    for(NSString *flag in @[@"ND2WSI_VIEWPORT_REPLAY",@"ND2WSI_VIEWPORT_AUTOQUIT",@"ND2WSI_VIEWPORT_CAPTURE_AFTER_REPLAY",@"MTL_CAPTURE_ENABLED"])
        [environment removeObjectForKey:flag];
    task.environment=environment;
    task.standardInput=NSFileHandle.fileHandleWithNullDevice;
    task.standardOutput=NSFileHandle.fileHandleWithNullDevice;
    task.standardError=NSFileHandle.fileHandleWithNullDevice;
    NSError *problem=nil;
    if(![task launchAndReturnError:&problem])[self recordError:problem.localizedDescription];
}
- (void)newUserWindow:(id)sender { (void)sender; [self launchWindowCommand:@"user"]; }
- (void)newAgentWindow:(id)sender { (void)sender; [self launchWindowCommand:@"agent"]; }
- (void)standardViewer:(id)sender { (void)sender; [self finishKind:@"handoff" failure:nil reason:nil]; }
- (void)checkUpdates:(id)sender { (void)sender; [self launchWindowCommand:@"updates"]; }
- (void)closeOwnWindow:(id)sender { (void)sender; [self.window performClose:nil]; }
- (void)installMenus {
    if(![self.session[@"window_commands"] count])return;
    NSMenu *main=[NSMenu new];
    NSMenuItem *appItem=[NSMenuItem new]; [main addItem:appItem];
    NSMenu *appMenu=[[NSMenu alloc] initWithTitle:@"nd2wsi-viewer"]; appItem.submenu=appMenu;
    NSMenuItem *update=[[NSMenuItem alloc] initWithTitle:@"Check for Updates…" action:@selector(checkUpdates:) keyEquivalent:@""];
    update.target=self; [appMenu addItem:update];
    [appMenu addItem:NSMenuItem.separatorItem];
    NSMenuItem *quit=[[NSMenuItem alloc] initWithTitle:@"Quit nd2wsi-viewer" action:@selector(closeOwnWindow:) keyEquivalent:@"q"];
    quit.target=self; [appMenu addItem:quit];
    NSMenuItem *fileItem=[NSMenuItem new]; [main addItem:fileItem];
    NSMenu *fileMenu=[[NSMenu alloc] initWithTitle:@"File"]; fileItem.submenu=fileMenu;
    NSArray *titles=@[@"New Window",@"New Agent Window",@"Open in Standard Viewer",@"Close Window"];
    NSArray *actions=@[NSStringFromSelector(@selector(newUserWindow:)),NSStringFromSelector(@selector(newAgentWindow:)),NSStringFromSelector(@selector(standardViewer:)),NSStringFromSelector(@selector(closeOwnWindow:))];
    NSArray *keys=@[@"n",@"n",@"",@"w"];
    for(NSUInteger i=0;i<titles.count;i++) {
        NSMenuItem *item=[[NSMenuItem alloc] initWithTitle:titles[i] action:NSSelectorFromString(actions[i]) keyEquivalent:keys[i]];
        item.target=self; item.keyEquivalentModifierMask=NSEventModifierFlagCommand|(i==1?NSEventModifierFlagShift:0);
        [fileMenu addItem:item];
    }
    NSApp.mainMenu=main;
}
- (BOOL)open:(NSError **)error {
    NSInteger viewportWidth=930,viewportHeight=800;
    if(!VPViewportDimension("ND2WSI_VIEWPORT_WIDTH",930,510,1600,&viewportWidth,error) ||
       !VPViewportDimension("ND2WSI_VIEWPORT_HEIGHT",800,400,1600,&viewportHeight,error))return NO;
    self.device=MTLCreateSystemDefaultDevice();
    if (!self.device || !self.device.hasUnifiedMemory) {
        self.outcome=[@{@"kind":@"fatal",@"failure_kind":@"gpu_device_unavailable",@"first_presented":@NO} mutableCopy];return NO;
    }
    self.pipeline=VPPipeline(self.device,error); if(!self.pipeline){
        self.outcome=[@{@"kind":@"fatal",@"failure_kind":@"gpu_pipeline_failure",@"first_presented":@NO} mutableCopy];return NO;
    }
    self.queue=[self.device newCommandQueue]; self.queue.label=@"ND2 direct viewport queue";
    if(!self.queue){self.outcome=[@{@"kind":@"fatal",@"failure_kind":@"memory_pressure",@"first_presented":@NO} mutableCopy];return NO;}
    // Benchmark overrides change actual native points, not DPR or a simulated
    // viewport. The 270-point controls column is outside the image viewport.
    self.window=[[NSWindow alloc] initWithContentRect:NSMakeRect(180,180,270+viewportWidth,viewportHeight)
        styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable|NSWindowStyleMaskResizable
        backing:NSBackingStoreBuffered defer:NO];
    NSString *role=[self.session[@"role"] isEqual:@"agent"] ? @"Agent" : @"User";
    self.window.title=[NSString stringWithFormat:@"nd2wsi-viewer — %@ · %@",role,self.session[@"id"]?:@"isolated"];
    self.window.delegate=self; self.window.releasedWhenClosed=NO;
    self.window.minSize=[self.window frameRectForContentRect:NSMakeRect(0,0,780,400)].size;
    NSView *content=self.window.contentView;
    self.view=[[VPView alloc] initWithFrame:NSZeroRect device:self.device];
    self.view.controller=self; self.view.delegate=self;
    self.view.colorPixelFormat=MTLPixelFormatBGRA8Unorm;
    self.view.depthStencilPixelFormat=MTLPixelFormatInvalid;
    self.view.sampleCount=1; self.view.framebufferOnly=YES;
    self.view.clearColor=MTLClearColorMake(.025,.025,.03,1);
    self.view.paused=YES; self.view.enableSetNeedsDisplay=YES;
    self.view.accessibilityLabel=@"Raw-channel Metal viewport";
    self.view.translatesAutoresizingMaskIntoConstraints=NO; [content addSubview:self.view];
    NSScrollView *scroll=[NSScrollView new]; scroll.translatesAutoresizingMaskIntoConstraints=NO;
    scroll.hasVerticalScroller=YES; scroll.drawsBackground=NO; [content addSubview:scroll];
    self.controls=[VPControls new]; self.controls.orientation=NSUserInterfaceLayoutOrientationVertical;
    self.controls.distribution=NSStackViewDistributionFill;
    [self.controls setHuggingPriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationVertical];
    self.controls.alignment=NSLayoutAttributeLeading; self.controls.spacing=8;
    self.controls.edgeInsets=NSEdgeInsetsMake(12,10,12,10); self.controls.translatesAutoresizingMaskIntoConstraints=NO;
    scroll.documentView=self.controls;
    [NSLayoutConstraint activateConstraints:@[
        [self.view.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
        [self.view.topAnchor constraintEqualToAnchor:content.topAnchor],
        [self.view.bottomAnchor constraintEqualToAnchor:content.bottomAnchor],
        [self.view.trailingAnchor constraintEqualToAnchor:scroll.leadingAnchor],
        [scroll.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
        [scroll.topAnchor constraintEqualToAnchor:content.topAnchor],
        [scroll.bottomAnchor constraintEqualToAnchor:content.bottomAnchor],
        [scroll.widthAnchor constraintEqualToConstant:270],
        [self.controls.widthAnchor constraintEqualToAnchor:scroll.widthAnchor constant:-14]
    ]];
    [self.controls addArrangedSubview:VPLabel(@"Native Metal · raw fluorescence")];
    [self.controls addArrangedSubview:VPLabel(@"Drag: pan  ·  Scroll / + −: zoom")];
    NSArray *titles=@[@"Fit Slide",@"Zoom In",@"Zoom Out",@"Replay Benchmark",@"Capture GPU Trace",@"Save Report"];
    NSArray *selectors=@[NSStringFromSelector(@selector(fit:)),NSStringFromSelector(@selector(zoomIn:)),NSStringFromSelector(@selector(zoomOut:)),NSStringFromSelector(@selector(replay:)),NSStringFromSelector(@selector(captureTrace:)),NSStringFromSelector(@selector(saveReportAction:))];
    for(NSUInteger i=0;i<titles.count;i++) {
        NSButton *button=[NSButton buttonWithTitle:titles[i] target:self action:NSSelectorFromString(selectors[i])];
        button.accessibilityLabel=titles[i]; [self.controls addArrangedSubview:button];
    }
    if([self.session[@"window_commands"] count]) {
        NSButton *standard=[NSButton buttonWithTitle:@"Open in Standard Viewer" target:self action:@selector(standardViewer:)];
        standard.accessibilityLabel=@"Open in Standard Viewer";
        [self.controls addArrangedSubview:standard];
        [self.controls addArrangedSubview:VPLabel(@"Annotations · measurements · export")];
        [self installMenus];
    }
    self.status=VPLabel(@"Loading isolated raw-tile session…");
    self.status.lineBreakMode=NSLineBreakByWordWrapping; self.status.maximumNumberOfLines=8;
    self.status.accessibilityLabel=@"Viewport diagnostics";
    [self.status.widthAnchor constraintEqualToConstant:236].active=YES;
    [self.controls addArrangedSubview:self.status];
    [self.window makeKeyAndOrderFront:nil]; [self.window makeFirstResponder:self.view];
    __weak VPController *weak=self;
    self.metadataTask=[self.network dataTaskWithURL:[self URLFor:@"metadata"] completionHandler:^(NSData *data,NSURLResponse *response,NSError *problem){
        dispatch_async(dispatch_get_main_queue(),^{
            VPController *owner=weak; if(!owner || owner.closed)return;
            if([owner fault:@"metadata_timeout"])return;
            if(problem || ![response isKindOfClass:NSHTTPURLResponse.class] || ((NSHTTPURLResponse *)response).statusCode!=200) {
                [owner finishKind:@"fatal" failure:problem.code==NSURLErrorTimedOut?@"metadata_timeout":@"metadata_invalid" reason:problem.localizedDescription?:@"Metadata request failed"]; return;
            }
            NSDictionary *meta=data.length && data.length<=2*1024*1024 ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
            if([owner fault:@"metadata_invalid"] || [owner fault:@"close_race"])meta=nil;
            [owner configure:meta];
        });
    }];[self.metadataTask resume];
    double timeout=[self fault:@"metadata_timeout"]?.15:20;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,(int64_t)(timeout*NSEC_PER_SEC)),dispatch_get_main_queue(),^{
        VPController *owner=weak;if(owner && !owner.closed && !owner.ready)
            [owner finishKind:@"fatal" failure:@"metadata_timeout" reason:@"Metadata response timed out"];
    });
    return YES;
}
- (void)configure:(NSDictionary *)meta {
    if(self.closed)return;
    if(!VPMetadataValid(meta)) {
        [self finishKind:@"fatal" failure:@"metadata_invalid" reason:@"Invalid raw fluorescence metadata"];return;
    }
    self.metadata=meta; self.imageWidth=[meta[@"width"] doubleValue]; self.imageHeight=[meta[@"height"] doubleValue];
    self.tileSize=[meta[@"tile_size"] unsignedIntegerValue]; self.channels=[meta[@"channels"] count];
    self.levels=[meta[@"levels"] sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *a,NSDictionary *b){return [a[@"downsample"] compare:b[@"downsample"]];}];
    for(NSUInteger c=0;c<self.channels;c++) {
        NSDictionary *channel=meta[@"channels"][c]; NSString *name=channel[@"label"]?:[NSString stringWithFormat:@"Channel %lu",(unsigned long)c];
        [self.initialColors addObject:channel[@"color"]];
        NSButton *check=[NSButton checkboxWithTitle:name target:self action:@selector(displayChanged:)];
        check.state=NSControlStateValueOn; check.tag=c; check.accessibilityLabel=[NSString stringWithFormat:@"Channel %lu visible %@",(unsigned long)c,name];
        [self.checks addObject:check]; [self.controls addArrangedSubview:check];
        NSArray *window=channel[@"window"];
        for(NSUInteger kind=0;kind<3;kind++) {
            NSString *kindName=kind==0?@"Window Low":kind==1?@"Window High":@"Gamma";
            NSStackView *row=[NSStackView new]; row.orientation=NSUserInterfaceLayoutOrientationHorizontal; row.spacing=6;
            NSTextField *label=VPLabel(kindName); [label.widthAnchor constraintEqualToConstant:76].active=YES; [row addArrangedSubview:label];
            double value=kind<2 && window.count==2 ? [window[kind] doubleValue] : kind==2?1:kind==0?0:65535;
            NSSlider *slider=[NSSlider sliderWithValue:value minValue:kind==2?.1:0 maxValue:kind==2?10:65535 target:self action:@selector(displayChanged:)];
            slider.continuous=YES; slider.tag=c; slider.accessibilityLabel=[NSString stringWithFormat:@"Channel %lu %@",(unsigned long)c,kindName];
            [slider.widthAnchor constraintEqualToConstant:142].active=YES; [row addArrangedSubview:slider];
            [(kind==0?self.lows:kind==1?self.highs:self.gammas) addObject:slider]; [self.controls addArrangedSubview:row];
        }
        NSPopUpButton *menu=[[NSPopUpButton alloc] initWithFrame:NSZeroRect pullsDown:NO];
        [menu addItemsWithTitles:@[@"Original LUT",@"White",@"Red",@"Green",@"Blue",@"Cyan",@"Magenta",@"Yellow"]];
        menu.target=self; menu.action=@selector(displayChanged:); menu.tag=c;
        menu.accessibilityLabel=[NSString stringWithFormat:@"Channel %lu color LUT",(unsigned long)c];
        [self.colorMenus addObject:menu]; [self.controls addArrangedSubview:menu];
    }
    self.ready=YES;
    NSDictionary *initial=self.session[@"initial_view_state"];
    // JSON null is the normal launcher representation of "no handoff", not
    // malformed user state. Keep genuinely supplied invalid state diagnostic.
    if((id)initial==NSNull.null)initial=nil;
    if(initial && VPStateValid(initial,self.imageWidth,self.imageHeight,self.channels)) {
        self.centerX=[initial[@"center"][0] doubleValue];self.centerY=[initial[@"center"][1] doubleValue];self.zoom=[initial[@"zoom"] doubleValue];
        for(NSUInteger c=0;c<self.channels;c++) {
            NSDictionary *channel=initial[@"channels"][c];
            self.lows[c].doubleValue=[channel[@"window"][0] doubleValue];self.highs[c].maxValue=65536;
            self.highs[c].doubleValue=[channel[@"window"][1] doubleValue];self.gammas[c].doubleValue=[channel[@"gamma"] doubleValue];
            self.checks[c].state=[channel[@"visible"] boolValue]?NSControlStateValueOn:NSControlStateValueOff;
            self.initialColors[c]=channel[@"color"];
        }[self viewportChanged:@"initial_view_state"];
    } else {
        if(initial)[self recordError:@"Invalid initial view state ignored; using fitted view"];
        [self fit:nil];
    }
    if(getenv("ND2WSI_VIEWPORT_REPLAY")) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),dispatch_get_main_queue(),^{if(!self.closed)[self replay:nil];});
    }
}
- (NSDictionary *)viewState {
    if(!self.ready)return nil;
    NSMutableArray *channels=[NSMutableArray new];VPTile *dummy=[VPTile new];VPUniforms uniforms=[self uniformsFor:dummy];
    for(NSUInteger c=0;c<self.channels;c++) {
        vector_float4 color=uniforms.colors[c], window=uniforms.windowGamma[c];
        // Preserve arbitrary user RGB values, rather than silently snapping the
        // browser's LUT to one of the native preset menu colors.
        NSArray *rgb=self.initialColors[c];
        if(self.colorMenus[c].indexOfSelectedItem>0)rgb=@[@(round(color.x*255)),@(round(color.y*255)),@(round(color.z*255))];
        [channels addObject:@{@"window":@[@(window.x),@(window.y)],@"gamma":@(window.z),@"color":rgb,@"visible":@((BOOL)(window.w>.5))}];
    }
    NSDictionary *state=@{@"version":@1,@"source_dimensions":@[@(self.imageWidth),@(self.imageHeight)],
        @"center":@[@(fmax(0,fmin(self.imageWidth,self.centerX))),@(fmax(0,fmin(self.imageHeight,self.centerY)))],@"zoom":@(self.zoom),@"channels":channels};
    return VPStateValid(state,self.imageWidth,self.imageHeight,self.channels)?state:nil;
}
- (void)fit:(id)sender {
    (void)sender; if(!self.ready)return;
    self.centerX=self.imageWidth/2; self.centerY=self.imageHeight/2;
    self.zoom=fmin(self.view.bounds.size.width/self.imageWidth,self.view.bounds.size.height/self.imageHeight)*.96;
    [self viewportChanged:@"fit"];
}
- (void)zoomIn:(id)sender { (void)sender; [self zoomAt:NSMakePoint(NSMidX(self.view.bounds),NSMidY(self.view.bounds)) delta:.4]; }
- (void)zoomOut:(id)sender { (void)sender; [self zoomAt:NSMakePoint(NSMidX(self.view.bounds),NSMidY(self.view.bounds)) delta:-.4]; }
- (void)pan:(NSPoint)delta {
    if(!self.ready)return; self.centerX-=delta.x/self.zoom; self.centerY+=delta.y/self.zoom;
    self.centerX=fmax(0,fmin(self.imageWidth,self.centerX)); self.centerY=fmax(0,fmin(self.imageHeight,self.centerY));
    [self viewportChanged:@"pan"];
}
- (void)zoomAt:(NSPoint)point delta:(double)delta {
    if(!self.ready)return;
    double px=point.x-NSMidX(self.view.bounds), py=NSMidY(self.view.bounds)-point.y;
    double worldX=self.centerX+px/self.zoom, worldY=self.centerY+py/self.zoom;
    double minimum=fmin(self.view.bounds.size.width/self.imageWidth,self.view.bounds.size.height/self.imageHeight)*.1;
    self.zoom=fmin(32,fmax(minimum,self.zoom*exp(delta)));
    self.centerX=worldX-px/self.zoom; self.centerY=worldY-py/self.zoom;
    [self viewportChanged:@"pointer_anchored_zoom"];
}
- (void)displayChanged:(id)sender {
    (void)sender; if(!self.ready)return;
    self.lastInput=CACurrentMediaTime(); self.actionID++; self.frameStreamed=NO;
    for(NSDictionary *item in self.wanted)if(!self.tiles[item[@"key"]])self.frameStreamed=YES;
    [self.actions addObject:@{@"kind":@"display_only",@"action_id":[self actionToken],@"time":@(self.lastInput),@"request_count":@(self.requestCount),@"uploaded_bytes":@(self.uploadedBytes)}];
    [self.view setNeedsDisplay:YES];
}
- (void)viewportChanged:(NSString *)action {
    if(!self.ready || self.closed)return;
    self.lastInput=CACurrentMediaTime(); self.generation++; self.actionID++;
    for(VPRequest *request in self.requests.allValues) {
        [self.retiringRequests addObject:request]; [request.task cancel]; self.cancelledRequests++;
    }
    // Cancellation is asynchronous. Old response/upload reservations remain
    // charged until completion releases their NSData, and count toward the cap.
    [self.requests removeAllObjects]; [self.failedKeys removeAllObjects];
    [self.retryCounts removeAllObjects]; [self.retryDeadlines removeAllObjects];
    double backing=self.view.drawableSize.width/fmax(1,self.view.bounds.size.width);
    double desired=1/fmax(1e-12,self.zoom*backing); self.activeLevel=0;
    for(NSUInteger i=0;i<self.levels.count;i++) if([self.levels[i][@"downsample"] doubleValue]<=desired)self.activeLevel=i;
    NSDictionary *level=self.levels[self.activeLevel]; double ds=[level[@"downsample"] doubleValue];
    double sx=self.imageWidth/[level[@"width"] doubleValue], sy=self.imageHeight/[level[@"height"] doubleValue];
    double left=self.centerX-self.view.bounds.size.width/(2*self.zoom), top=self.centerY-self.view.bounds.size.height/(2*self.zoom);
    double right=self.centerX+self.view.bounds.size.width/(2*self.zoom), bottom=self.centerY+self.view.bounds.size.height/(2*self.zoom);
    NSInteger columns=([level[@"width"] integerValue]+self.tileSize-1)/self.tileSize;
    NSInteger rows=([level[@"height"] integerValue]+self.tileSize-1)/self.tileSize;
    NSInteger x0=MAX(0,(NSInteger)floor(left/sx/self.tileSize)), y0=MAX(0,(NSInteger)floor(top/sy/self.tileSize));
    NSInteger x1=MIN(columns-1,(NSInteger)floor(nextafter(right,-INFINITY)/sx/self.tileSize));
    NSInteger y1=MIN(rows-1,(NSInteger)floor(nextafter(bottom,-INFINITY)/sy/self.tileSize));
    [self.wanted removeAllObjects]; self.frameStreamed=NO;
    for(NSInteger y=y0;y<=y1;y++)for(NSInteger x=x0;x<=x1;x++) {
        NSString *key=[NSString stringWithFormat:@"%lu/%ld/%ld",(unsigned long)self.activeLevel,(long)x,(long)y];
        NSUInteger w=MIN(self.tileSize,[level[@"width"] unsignedIntegerValue]-x*self.tileSize);
        NSUInteger h=MIN(self.tileSize,[level[@"height"] unsignedIntegerValue]-y*self.tileSize);
        [self.wanted addObject:@{@"key":key,@"tx":@(x),@"ty":@(y),@"width":@(w),@"height":@(h),@"level":@(self.activeLevel)}];
        if(!self.tiles[key])self.frameStreamed=YES;
    }
    [self.wanted sortUsingComparator:^NSComparisonResult(NSDictionary *a,NSDictionary *b){
        double da=pow(([a[@"tx"] doubleValue]+.5)*self.tileSize*ds-self.centerX,2)+pow(([a[@"ty"] doubleValue]+.5)*self.tileSize*ds-self.centerY,2);
        double db=pow(([b[@"tx"] doubleValue]+.5)*self.tileSize*ds-self.centerX,2)+pow(([b[@"ty"] doubleValue]+.5)*self.tileSize*ds-self.centerY,2);
        return da<db?NSOrderedAscending:da>db?NSOrderedDescending:NSOrderedSame;
    }];
    [self.actions addObject:@{@"kind":action,@"action_id":[self actionToken],@"time":@(self.lastInput),@"center":@[@(self.centerX),@(self.centerY)],@"zoom":@(self.zoom),@"level":level[@"path"],@"required_tiles":@(self.wanted.count),@"class":self.frameStreamed?@"streamed":@"resident"}];
    [self scheduleTiles]; [self.view setNeedsDisplay:YES];
}
- (BOOL)reserve:(uint64_t)bytes {
    // NSData response + shared Metal input can coexist during upload. Reserve
    // both, not just resident buffers. No GPU-in-use buffer can be evicted.
    while(self.residentBytes+self.pendingBytes+bytes>MEMORY_BUDGET) {
        VPTile *oldest=nil;
        for(VPTile *tile in self.tiles.allValues) {
            if(tile.gpuUsers)continue;
            BOOL visible=NO; for(NSDictionary *item in self.wanted)if([item[@"key"] isEqual:tile.key]){visible=YES;break;}
            if(visible)continue;
            if(!oldest || tile.lastUse<oldest.lastUse)oldest=tile;
        }
        if(!oldest)return NO;
        self.residentBytes-=oldest.bytes; [self.tiles removeObjectForKey:oldest.key];
    } return YES;
}
- (void)scheduleTiles {
    if(self.closed)return;
    // HTTP cancellation does not stop an already-running decoder in the
    // isolated backend. Keep a global cooldown across pan generations so new
    // pointer events cannot reset per-key retries into a rejection storm.
    if(CACurrentMediaTime()<self.backpressureUntil) {
        [self wakeSchedulerAt:self.backpressureUntil generation:self.generation]; return;
    }
    for(NSDictionary *item in self.wanted) {
        if(self.requests.count+self.retiringRequests.count>=MAX_REQUESTS)break;
        NSString *key=item[@"key"]; if(self.tiles[key] || self.requests[key] || [self.failedKeys containsObject:key])continue;
        double deadline=[self.retryDeadlines[key] doubleValue];
        if(deadline>CACurrentMediaTime()) {
            [self wakeSchedulerAt:deadline generation:self.generation]; continue;
        }
        uint64_t bytes=[item[@"width"] unsignedLongLongValue]*[item[@"height"] unsignedLongLongValue]*self.channels*2;
        if(![self reserve:bytes*2])continue;
        VPRequest *request=[VPRequest new]; request.bytes=bytes*2; request.generation=self.generation;
        self.requests[key]=request; self.pendingBytes+=request.bytes; self.requestCount++;
        self.peakBytes=MAX(self.peakBytes,self.residentBytes+self.pendingBytes);
        NSURLComponents *parts=[NSURLComponents componentsWithURL:[self URLFor:@"tile"] resolvingAgainstBaseURL:YES];
        parts.queryItems=@[[NSURLQueryItem queryItemWithName:@"level" value:self.levels[[item[@"level"] unsignedIntegerValue]][@"path"]],
                          [NSURLQueryItem queryItemWithName:@"tx" value:[item[@"tx"] stringValue]],
                          [NSURLQueryItem queryItemWithName:@"ty" value:[item[@"ty"] stringValue]]];
        __weak VPController *weak=self;
        request.task=[self.network dataTaskWithURL:parts.URL completionHandler:^(NSData *data,NSURLResponse *response,NSError *problem){
            dispatch_async(dispatch_get_main_queue(),^{
                VPController *owner=weak; if(!owner || owner.closed)return;
                if([owner.retiringRequests containsObject:request]) {
                    [owner.retiringRequests removeObject:request]; owner.pendingBytes-=request.bytes;
                    dispatch_async(dispatch_get_main_queue(),^{[owner scheduleTiles];}); return;
                }
                if(owner.requests[key]!=request || request.generation!=owner.generation)return;
                NSInteger status=[response isKindOfClass:NSHTTPURLResponse.class]?((NSHTTPURLResponse *)response).statusCode:0;
                if([owner fault:@"all_tiles_failed"])status=500;
                if([owner fault:@"tile_503"] && [owner.retryCounts[key] unsignedIntegerValue]<2)status=503;
                // Reservation remains in force through response/buffer overlap.
                if(!problem && status==200 && data.length==bytes) {
                    id<MTLBuffer> buffer=[owner.device newBufferWithBytes:data.bytes length:bytes options:MTLResourceStorageModeShared];
                    if(buffer) {
                        buffer.label=[@"Raw CYX ushort tile " stringByAppendingString:key];
                        VPTile *tile=[VPTile new]; tile.key=key; tile.buffer=buffer; tile.bytes=bytes;
                        tile.tx=[item[@"tx"] unsignedIntegerValue]; tile.ty=[item[@"ty"] unsignedIntegerValue];
                        tile.width=[item[@"width"] unsignedIntegerValue]; tile.height=[item[@"height"] unsignedIntegerValue];
                        tile.level=[item[@"level"] unsignedIntegerValue]; tile.lastUse=CACurrentMediaTime();
                        owner.tiles[key]=tile; owner.residentBytes+=bytes; owner.uploadedBytes+=bytes;
                    } else { owner.memoryFailures++;[owner.failedKeys addObject:key]; [owner recordError:@"Shared tile buffer allocation failed"]; }
                } else if(!problem && status==503) {
                    owner.backpressureResponses++;
                    NSUInteger attempts=[owner.retryCounts[key] unsignedIntegerValue];
                    if(attempts<MAX_BACKPRESSURE_RETRIES) {
                        double delay=.1*(1ull<<attempts);
                        double next=CACurrentMediaTime()+delay;
                        owner.retryCounts[key]=@(attempts+1); owner.retryDeadlines[key]=@(next);
                        owner.backpressureUntil=MAX(owner.backpressureUntil,next);
                        owner.backpressureRetries++;
                        [owner wakeSchedulerAt:next generation:owner.generation];
                    } else {
                        owner.exhaustedRetries++; [owner.failedKeys addObject:key];
                        [owner recordError:[NSString stringWithFormat:@"Raw tile %@: decoder busy after %d bounded retries",key,MAX_BACKPRESSURE_RETRIES]];
                    }
                } else if(problem.code!=NSURLErrorCancelled) {
                    [owner.failedKeys addObject:key];
                    [owner recordError:[NSString stringWithFormat:@"Raw tile %@ failed: %@",key,problem.localizedDescription?:@"status or byte-length mismatch"]];
                }
                owner.pendingBytes-=request.bytes; [owner.requests removeObjectForKey:key];
                [owner updateStatus]; [owner.view setNeedsDisplay:YES];
                // Dispatching after this block releases its NSData before another
                // upload can spend the same reservation.
                dispatch_async(dispatch_get_main_queue(),^{[owner scheduleTiles];});
            });
        }]; [request.task resume];
    } [self updateStatus];
    if(!self.firstPresented && self.wanted.count && !self.requests.count && !self.retiringRequests.count) {
        BOOL exhausted=YES;
        for(NSDictionary *item in self.wanted)if(![self.failedKeys containsObject:item[@"key"]]){exhausted=NO;break;}
        if(exhausted)[self finishKind:@"fatal" failure:self.memoryFailures?@"memory_pressure":@"tile_unavailable" reason:@"No first-screen image tile could be loaded after bounded retries"];
    }
}
- (void)wakeSchedulerAt:(double)deadline generation:(uint64_t)generation {
    // Capture only a weak owner and scalars: retry timers must not extend the
    // completed request/NSData lifetime after releasing its memory reservation.
    __weak VPController *weak=self;
    int64_t delay=(int64_t)(MAX(.001,deadline-CACurrentMediaTime())*NSEC_PER_SEC);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,delay),dispatch_get_main_queue(),^{
        VPController *owner=weak;
        if(owner && !owner.closed && owner.generation==generation)[owner scheduleTiles];
    });
}
- (void)updateStatus {
    id footprint=VPProcessMemory()[@"physical_footprint_bytes"];
    if([footprint isKindOfClass:NSNumber.class])self.peakFootprint=MAX(self.peakFootprint,[footprint unsignedLongLongValue]);
    NSUInteger available=0; for(NSDictionary *item in self.wanted)if(self.tiles[item[@"key"]])available++;
    self.status.stringValue=[NSString stringWithFormat:@"Level %@ · %.4g×\nCenter %.1f, %.1f\nVisible %lu/%lu · resident %.1f MiB\nIn flight %lu · presented %llu\nOne pass · CPU readback 0%@",
        self.ready?self.levels[self.activeLevel][@"path"]:@"—",self.zoom,self.centerX,self.centerY,
        (unsigned long)available,(unsigned long)self.wanted.count,self.residentBytes/1048576.,
        (unsigned long)self.requests.count,self.presentedCount,self.replaying?@"\nBenchmark replay active":@""];
}
- (VPUniforms)uniformsFor:(VPTile *)tile {
    VPUniforms u={0}; u.dimensions=(vector_uint4){(uint32_t)tile.width,(uint32_t)tile.height,(uint32_t)self.channels,0};
    static const float presets[7][3]={{255,255,255},{255,0,0},{0,255,0},{0,0,255},{0,255,255},{255,0,255},{255,255,0}};
    for(NSUInteger c=0;c<self.channels;c++) {
        float low=self.lows[c].floatValue;
        float high=self.highs[c].floatValue;
        if(high<=low)high=low+1; // Existing browser parse_windows convention.
        u.windowGamma[c]=(vector_float4){low,high,self.gammas[c].floatValue,self.checks[c].state==NSControlStateValueOn?1:0};
        NSInteger selected=self.colorMenus[c].indexOfSelectedItem;
        NSArray *color=self.initialColors[c];
        if(selected>0)u.colors[c]=(vector_float4){presets[selected-1][0]/255.f,presets[selected-1][1]/255.f,presets[selected-1][2]/255.f,0};
        else if(color.count==3)u.colors[c]=(vector_float4){[color[0] floatValue]/255.f,[color[1] floatValue]/255.f,[color[2] floatValue]/255.f,0};
        else u.colors[c]=(vector_float4){1,1,1,0};
    } return u;
}
- (void)drawInMTKView:(MTKView *)view {
    if(!self.ready || self.closed)return;
    // A diagnostic trace contains exactly one production frame even when raw
    // requests finish during capture. Resume the latest view after stopping it.
    if(self.captureRunning)return;
    if(self.activeCommands>=3) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_MSEC),dispatch_get_main_queue(),^{if(!self.closed)[self.view setNeedsDisplay:YES];}); return;
    }
    BOOL captureThis=NO;
    if(self.capturePending && !self.captureRunning) {
        self.capturePending=NO; MTLCaptureManager *manager=MTLCaptureManager.sharedCaptureManager;
        NSError *problem=nil;
        if([manager supportsDestination:MTLCaptureDestinationGPUTraceDocument]) {
            NSString *directory=[self.reportPath stringByDeletingLastPathComponent];
            NSString *path=[directory stringByAppendingPathComponent:[NSString stringWithFormat:@"viewport-%@.gputrace",NSUUID.UUID.UUIDString]];
            MTLCaptureDescriptor *descriptor=[MTLCaptureDescriptor new]; descriptor.captureObject=self.queue;
            descriptor.destination=MTLCaptureDestinationGPUTraceDocument; descriptor.outputURL=[NSURL fileURLWithPath:path];
            if([manager startCaptureWithDescriptor:descriptor error:&problem]) {
                self.captureRunning=YES; captureThis=YES; self.capture[@"path"]=path;
            }
        }
        if(!captureThis)self.capture[@"error"]=problem.localizedDescription?:@"GPU trace capture unsupported or MTL_CAPTURE_ENABLED not set";
    }
    double encodeStart=CACurrentMediaTime();
    id<CAMetalDrawable> drawable=view.currentDrawable;
    MTLRenderPassDescriptor *pass=view.currentRenderPassDescriptor;
    if(!drawable || !pass) {
        if(captureThis){[MTLCaptureManager.sharedCaptureManager stopCapture];self.captureRunning=NO;self.capture[@"error"]=@"No drawable";} return;
    }
    pass.colorAttachments[0].loadAction=MTLLoadActionClear; pass.colorAttachments[0].storeAction=MTLStoreActionStore;
    id<MTLCommandBuffer> command=[self.queue commandBuffer]; command.label=@"Viewport frame — one fused raw-channel render pass";
    id<MTLRenderCommandEncoder> encoder=[command renderCommandEncoderWithDescriptor:pass];
    if(!command || !encoder){[self finishKind:@"fatal" failure:@"memory_pressure" reason:@"Metal command allocation failed"];return;}
    encoder.label=@"Viewport clear/store; no intermediate attachments; no CPU readback";
    [encoder setRenderPipelineState:self.pipeline];
    NSMutableArray<VPTile *> *retained=[NSMutableArray new];
    double sx=self.imageWidth/[self.levels[self.activeLevel][@"width"] doubleValue];
    double sy=self.imageHeight/[self.levels[self.activeLevel][@"height"] doubleValue];
    double halfW=fmax(1,view.bounds.size.width/2), halfH=fmax(1,view.bounds.size.height/2);
    for(NSDictionary *item in self.wanted) {
        VPTile *tile=self.tiles[item[@"key"]]; if(!tile)continue;
        double x0=tile.tx*self.tileSize*sx, y0=tile.ty*self.tileSize*sy;
        double x1=fmin(self.imageWidth,x0+tile.width*sx), y1=fmin(self.imageHeight,y0+tile.height*sy);
        float left=(x0-self.centerX)*self.zoom/halfW, right=(x1-self.centerX)*self.zoom/halfW;
        float top=-(y0-self.centerY)*self.zoom/halfH, bottom=-(y1-self.centerY)*self.zoom/halfH;
        float w=tile.width,h=tile.height;
        VPVertex vertices[6]={{{left,top,0,0}},{{left,bottom,0,h}},{{right,bottom,w,h}},
                               {{left,top,0,0}},{{right,bottom,w,h}},{{right,top,w,0}}};
        VPUniforms uniform=[self uniformsFor:tile];
        [encoder setVertexBytes:vertices length:sizeof(vertices) atIndex:0];
        [encoder setFragmentBuffer:tile.buffer offset:0 atIndex:0];
        [encoder setFragmentBytes:&uniform length:sizeof(uniform) atIndex:1];
        [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:6];
        tile.gpuUsers++; tile.lastUse=encodeStart; [retained addObject:tile];
    }
    [encoder endEncoding];
    NSMutableDictionary *frame=[@{@"class":self.frameStreamed?@"streamed":@"resident",@"mode":captureThis?@"capture":self.mode,
        @"input_time":@(self.lastInput),@"encode_start":@(encodeStart),@"cpu_encode_ms":@((CACurrentMediaTime()-encodeStart)*1000),
        @"render_passes":@1,@"tiles_drawn":@(retained.count),@"tiles_required":@(self.wanted.count),
        @"request_count":@(self.requestCount),@"uploaded_bytes":@(self.uploadedBytes),
        @"complete_viewport":@(retained.count==self.wanted.count),@"level":self.levels[self.activeLevel][@"path"],
        @"generation":@(self.generation),@"action_id":[self actionToken],@"load_action":@"clear",@"store_action":@"store",@"cpu_readbacks":@0} mutableCopy];
    if(self.frames.count<20000)[self.frames addObject:frame];
    self.activeCommands++; self.encodeCount++;
    __weak VPController *weak=self;
    [drawable addPresentedHandler:^(id<MTLDrawable> shown){
        double presented=shown.presentedTime;
        dispatch_async(dispatch_get_main_queue(),^{
            VPController *owner=weak;if(!owner)return;
            frame[@"presented_time"]=@(presented); frame[@"dropped"]=@(presented<=0);
            if(presented>0 && [frame[@"input_time"] doubleValue]>0)frame[@"input_to_present_ms"]=@((presented-[frame[@"input_time"] doubleValue])*1000);
            if(presented>0 && owner.previousPresentation>0)frame[@"presentation_interval_ms"]=@((presented-owner.previousPresentation)*1000);
            if(presented>0)owner.previousPresentation=presented;
            if(presented>0)owner.presentedCount++; else owner.droppedCount++;
            [owner confirmFirstPresentation:frame];
            [owner updateStatus];
        });
    }];
    [command addCompletedHandler:^(id<MTLCommandBuffer> done){
        dispatch_async(dispatch_get_main_queue(),^{
            VPController *owner=weak;if(!owner)return;
            for(VPTile *tile in retained)tile.gpuUsers--;
            owner.activeCommands--;
            frame[@"gpu_ms"]=@(MAX(0,(done.GPUEndTime-done.GPUStartTime)*1000));
            frame[@"gpu_start"]=@(done.GPUStartTime); frame[@"gpu_end"]=@(done.GPUEndTime);
            frame[@"gpu_status"]=done.status==MTLCommandBufferStatusCompleted?@"completed":@"failed";
            BOOL injected=([owner fault:@"gpu_fatal"] || [owner fault:@"gpu_memory_pressure"]) && retained.count && !owner.faultUsed;
            if(injected)owner.faultUsed=YES;
            if(injected)frame[@"gpu_status"]=@"failed";
            [owner confirmFirstPresentation:frame];
            if(done.status!=MTLCommandBufferStatusCompleted || injected) {
                BOOL memory=([done.error.domain isEqual:MTLCommandBufferErrorDomain] && done.error.code==MTLCommandBufferErrorOutOfMemory) || [owner fault:@"gpu_memory_pressure"];
                [owner finishKind:@"fatal" failure:memory?@"memory_pressure":@"gpu_execution_failure" reason:injected?@"Diagnostic injected GPU execution failure":done.error.localizedDescription];
            }
            if(captureThis) {
                [MTLCaptureManager.sharedCaptureManager stopCapture]; owner.captureRunning=NO;
                owner.capture[@"completed"]=@(done.status==MTLCommandBufferStatusCompleted);
                owner.capture[@"encoded_passes"]=@1; owner.capture[@"load_action"]=@"clear"; owner.capture[@"store_action"]=@"store";
                owner.capture[@"intermediate_attachments"]=@0; owner.capture[@"production_cpu_readbacks"]=@0;
                [owner saveReport];
                if(!owner.closed)[owner.view setNeedsDisplay:YES];
            }
            [owner scheduleTiles];
            [owner finishIfDrained];
        });
    }];
    [command presentDrawable:drawable]; [command commit];
}
- (void)mtkView:(MTKView *)view drawableSizeWillChange:(CGSize)size {
    (void)view;(void)size; if(self.ready)[self viewportChanged:@"resize"];
}
- (void)captureTrace:(id)sender {
    (void)sender; self.capture[@"requested"]=@YES; self.capturePending=YES;
    self.lastInput=CACurrentMediaTime(); self.actionID++; [self.view setNeedsDisplay:YES];
}
- (void)saveReportAction:(id)sender { (void)sender; [self refreshBackendAndSave]; }
- (void)refreshBackendAndSave {
    __weak VPController *weak=self;
    [[self.network dataTaskWithURL:[self URLFor:@"metrics"] completionHandler:^(NSData *data,NSURLResponse *response,NSError *error){
        (void)response;
        dispatch_async(dispatch_get_main_queue(),^{
            VPController *owner=weak;if(!owner)return;
            if(data && !error)owner.backendMetrics=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
            [owner saveReport];
        });
    }] resume];
}
- (void)replay:(id)sender {
    (void)sender;if(!self.ready || self.replaying)return;
    self.replaying=YES; self.replayStep=0;self.replayWaitStart=CACurrentMediaTime();self.mode=@"benchmark";
    if([self.session[@"role"] isEqual:@"agent"] && !self.replayInputMonitor) {
        // This local monitor consumes only physical input directed at this
        // diagnostic Agent window. Other apps/windows remain interactive, no
        // click-through is enabled, and programmatic replay calls are unchanged.
        NSEventMask mask=NSEventMaskLeftMouseDown|NSEventMaskLeftMouseUp|NSEventMaskRightMouseDown|
            NSEventMaskRightMouseUp|NSEventMaskOtherMouseDown|NSEventMaskOtherMouseUp|
            NSEventMaskLeftMouseDragged|NSEventMaskRightMouseDragged|NSEventMaskOtherMouseDragged|
            NSEventMaskMouseMoved|NSEventMaskScrollWheel|NSEventMaskMagnify|NSEventMaskRotate|
            NSEventMaskSwipe|NSEventMaskKeyDown|NSEventMaskKeyUp|NSEventMaskFlagsChanged;
        __weak VPController *weak=self;
        self.replayInputMonitor=[NSEvent addLocalMonitorForEventsMatchingMask:mask handler:^NSEvent *(NSEvent *event){
            VPController *owner=weak;
            if(owner && VPBlocksReplayInput(owner.replaying,[owner.session[@"role"] isEqual:@"agent"],event.window==owner.window))return nil;
            return event;
        }];
    }
    [self.actions addObject:@{@"kind":@"replay_start",@"time":@(CACurrentMediaTime()),@"viewport_points":@[@(self.view.bounds.size.width),@(self.view.bounds.size.height)],@"drawable_pixels":@[@(self.view.drawableSize.width),@(self.view.drawableSize.height)]}];
    [self replayNext];
}
- (void)stopReplayInputMonitor {
    if(self.replayInputMonitor){[NSEvent removeMonitor:self.replayInputMonitor];self.replayInputMonitor=nil;}
}
- (NSString *)actionToken {
    return self.replaying && self.replayStep ? [NSString stringWithFormat:@"step-%lu",(unsigned long)(self.replayStep-1)] : [NSString stringWithFormat:@"interaction-%llu",self.actionID];
}
- (void)replayNext {
    if(self.closed || !self.replaying)return;
    NSUInteger available=0;for(NSDictionary *item in self.wanted)if(self.tiles[item[@"key"]])available++;
    if((self.requests.count || available<self.wanted.count) && CACurrentMediaTime()-self.replayWaitStart<20) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,100*NSEC_PER_MSEC),dispatch_get_main_queue(),^{[self replayNext];});return;
    }
    NSUInteger step=self.replayStep++;
    if(step==0)[self fit:nil];
    else if(step<=20) {
        // The viewport and raw tiles are unchanged: only uniforms are updated.
        self.gammas[0].doubleValue=1+(step%5)*.25; [self displayChanged:nil];
    } else if(step==21){self.checks[0].state=NSControlStateValueOff;[self displayChanged:nil];}
    else if(step==22){self.checks[0].state=NSControlStateValueOn;[self displayChanged:nil];}
    else if(step==23){self.gammas[0].doubleValue=1;[self displayChanged:nil];}
    else if(step<=29) {
        [self zoomAt:NSMakePoint(self.view.bounds.size.width*.37,self.view.bounds.size.height*.61) delta:.55];
    } else if(step<=37) {
        [self pan:NSMakePoint((step%2?1:-1)*self.view.bounds.size.width*.58,self.view.bounds.size.height*.31)];
    } else if(step<=42) {
        [self zoomAt:NSMakePoint(self.view.bounds.size.width*.63,self.view.bounds.size.height*.29) delta:-.6];
    } else if(step==43){[self fit:nil];}
    else {
        self.replaying=NO;self.mode=@"interactive";
        [self stopReplayInputMonitor];
        [self.actions addObject:@{@"kind":@"replay_complete",@"time":@(CACurrentMediaTime()),@"steps":@(step)}];
        [self refreshBackendAndSave];[self updateStatus];
        // Capture is deliberately outside the timed replay. It has its own
        // mode and cannot contaminate the benchmark latency summaries.
        if(getenv("ND2WSI_VIEWPORT_CAPTURE_AFTER_REPLAY"))[self captureTrace:nil];
        if(getenv("ND2WSI_VIEWPORT_AUTOQUIT"))dispatch_after(dispatch_time(DISPATCH_TIME_NOW,3*NSEC_PER_SEC),dispatch_get_main_queue(),^{[self.window close];});
        return;
    }
    self.replayWaitStart=CACurrentMediaTime();
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,100*NSEC_PER_MSEC),dispatch_get_main_queue(),^{[self replayNext];});
}
- (void)saveReport {
    if(!self.reportPath.length)return;
    NSMutableDictionary *summary=[NSMutableDictionary new];
    BOOL benchmark=NO; for(NSDictionary *frame in self.frames)if([frame[@"mode"] isEqual:@"benchmark"])benchmark=YES;
    for(NSString *kind in @[@"resident",@"streamed"]) {
        NSMutableDictionary *statistics=[NSMutableDictionary new];
        for(NSString *metric in @[@"input_to_present_ms",@"gpu_ms",@"cpu_encode_ms",@"presentation_interval_ms"]) {
            NSMutableArray *values=[NSMutableArray new];
            NSMutableSet *seen=[NSMutableSet new];
            for(NSDictionary *frame in self.frames)if([frame[@"class"] isEqual:kind] && [frame[@"complete_viewport"] boolValue] && [frame[@"presented_time"] doubleValue]>0 && frame[metric] &&
                ![frame[@"mode"] isEqual:@"capture"] && (!benchmark || [frame[@"mode"] isEqual:@"benchmark"]) && ![seen containsObject:frame[@"action_id"]]) {
                [seen addObject:frame[@"action_id"]]; [values addObject:frame[metric]];
            }
            statistics[metric]=VPSummary(values);
        } summary[kind]=statistics;
    }
    NSMutableArray *counters=[NSMutableArray new];
    for(id<MTLCounterSet> set in self.device.counterSets) {
        NSMutableArray *names=[NSMutableArray new]; for(id<MTLCounter> counter in set.counters)[names addObject:counter.name];
        [counters addObject:@{@"set":set.name,@"counters":names}];
    }
    NSMutableDictionary *memory=[VPProcessMemory() mutableCopy]; memory[@"peak_physical_footprint_bytes"]=@(self.peakFootprint);
    NSDictionary *state=self.outcome[@"view_state"]?:[self viewState];
    NSDictionary *report=@{@"schema":@"nd2wsi-native-viewport-v1",@"session":self.session?:@{},
        @"open_attempt_id":self.session[@"open_attempt_id"]?:@"",@"fallback_consumed":self.session[@"fallback_consumed"]?:@NO,
        @"outcome":self.outcome?:@{@"kind":@"running",@"first_presented":@(self.firstPresented)},@"view_state":state?:[NSNull null],
        @"lifecycle":@{@"stopping":@(self.stopping),@"finalized":@(self.finalized),@"active_gpu_commands":@(self.activeCommands)},
        @"context":self.session[@"context"]?:@{},@"timing_kind":@"metal_drawable_presented",
        @"resource":memory,
        @"source":[self.metadata[@"source"] lastPathComponent]?:@"",@"device":self.device.name?:@"",
        @"unified_memory":@(self.device.hasUnifiedMemory),@"backend":self.backendMetrics?:@{},
        @"scope":@"Direct fluorescence display. Input includes source packing and shared-buffer copies plus possible HTTP/NSData copies; no production CPU pixel readback.",
        @"viewport":@{@"points":@[@(self.view.bounds.size.width),@(self.view.bounds.size.height)],@"drawable_pixels":@[@(self.view.drawableSize.width),@(self.view.drawableSize.height)],@"center":@[@(self.centerX),@(self.centerY)],@"zoom":@(self.zoom),@"level":self.ready?self.levels[self.activeLevel][@"path"]:@""},
        @"memory":@{@"tile_pipeline_budget_bytes":@(MEMORY_BUDGET),@"resident_bytes":@(self.residentBytes),@"inflight_reserved_bytes":@(self.pendingBytes),@"peak_resident_plus_inflight_reserved_bytes":@(self.peakBytes),@"scope":@"Raw tile buffers plus two-times HTTP in-flight payload reservation; excludes drawable textures, framework caches and backend process."},
        @"transfers":@{@"raw_tile_requests":@(self.requestCount),@"cancelled_requests":@(self.cancelledRequests),@"explicit_shared_buffer_input_copy_bytes":@(self.uploadedBytes),@"production_cpu_pixel_readbacks":@0,@"cpu_rgb_or_jpeg_frames":@0},
        @"backpressure":@{@"http_503_responses":@(self.backpressureResponses),@"retries_scheduled":@(self.backpressureRetries),@"exhausted_tile_retries":@(self.exhaustedRetries),@"max_retries_per_tile_generation":@(MAX_BACKPRESSURE_RETRIES),@"retry_delays_ms":@[@100,@200,@400,@800,@1600]},
        @"render_contract":@{@"passes_per_frame":@1,@"color_format":@"bgra8Unorm",@"load":@"clear",@"store":@"store",@"intermediate_attachments":@0,@"sampling":@"nearest raw pixel at selected pyramid level",@"synchronous_tile_gpu_waits":@0},
        @"timings":summary,@"timing_note":@"Presented-handler latency and GPU timings are separate. Presentation interval includes on-demand idle/action cadence, not saturation FPS. First complete presented frame per action enters summaries; replay supersedes interactive samples when present. GPU captures are excluded. Full records preserve partial streamed draws.",
        @"bandwidth":@{@"measured":@NO,@"bytes_per_tile":[NSNull null],@"note":@"Software input-copy counters are not hardware system-memory bandwidth.",@"available_metal_counter_sets":counters},
        @"capture":self.capture,@"frames":self.frames,@"actions":self.actions,@"errors":self.errors,
        @"presented_frames":@(self.presentedCount),@"dropped_presented_callbacks":@(self.droppedCount),@"encoded_frames":@(self.encodeCount)};
    NSError *error=nil;
    NSData *data=[NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingPrettyPrinted|NSJSONWritingSortedKeys error:&error];
    if(data)[data writeToFile:self.reportPath options:NSDataWritingAtomic error:&error];
    if(error)fprintf(stderr,"Metal viewport report: %s\n",error.localizedDescription.UTF8String);
}
- (void)windowWillClose:(NSNotification *)notification {
    (void)notification;if(!self.internalClose)[self finishKind:@"closed" failure:nil reason:nil];
}
@end

API int nd2wsi_viewport_run(const char *base_url,const char *session_json,const char *report_path) {
    @autoreleasepool {
        if(!NSThread.isMainThread || !base_url || !session_json || !report_path)return 1;
        NSString *base=[NSString stringWithUTF8String:base_url];
        NSURL *url=[NSURL URLWithString:base];
        // The launcher provides an unguessable loopback capability URL. Never
        // fetch remote slide payloads or let a native window adopt another role.
        if(![url.scheme isEqual:@"http"] || ![@[@"127.0.0.1",@"localhost",@"::1"] containsObject:url.host] || ![base hasSuffix:@"/"])return 2;
        NSData *json=[[NSString stringWithUTF8String:session_json] dataUsingEncoding:NSUTF8StringEncoding];
        NSDictionary *session=[NSJSONSerialization JSONObjectWithData:json options:0 error:nil];
        if(![session isKindOfClass:NSDictionary.class] || ![session[@"id"] isKindOfClass:NSString.class] || ![@[@"agent",@"user"] containsObject:session[@"role"]])return 3;
        [NSApplication sharedApplication]; [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        VPController *controller=[VPController new]; controller.baseURL=base;controller.session=session;
        controller.reportPath=[NSString stringWithUTF8String:report_path]; NSApp.delegate=controller;
        NSError *error=nil;
        if(![controller open:&error]){
            if(!controller.outcome)controller.outcome=[@{@"kind":@"fatal",@"failure_kind":@"renderer_initialization_failure",@"first_presented":@NO} mutableCopy];
            [controller.network invalidateAndCancel];[controller saveReport];
            fprintf(stderr,"Metal viewport: %s\n",error.localizedDescription.UTF8String?:"No unified-memory Metal device");return 4;
        }
        [NSApp activateIgnoringOtherApps:NO]; [NSApp run];
        [controller saveReport]; return [controller.outcome[@"kind"] isEqual:@"fatal"]?5:0;
    }
}
