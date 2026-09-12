// macOS / Apple silicon experimental exact 2x2 pyramid reducer.
//
// The caller owns both page-aligned, page-rounded anonymous mmap allocations.
// They must remain mapped and untouched for this synchronous call. This bridge
// wraps those allocations; it does not claim disk-to-display zero-copy.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <dispatch/dispatch.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define ND2WSI_EXPORT __attribute__((visibility("default")))

static id<MTLDevice> device;
static id<MTLCommandQueue> queue;
static id<MTLComputePipelineState> pipeline8;
static id<MTLComputePipelineState> pipeline16;
static NSString *initializationError;
static dispatch_once_t initializationOnce;
static pthread_mutex_t reductionMutex = PTHREAD_MUTEX_INITIALIZER;

typedef struct {
    uint32_t channels;
    uint32_t height;
    uint32_t width;
} ReductionDimensions;

// Accumulation deliberately uses uint, not ushort/half: 4 * 65535 needs
// 18 bits, and half cannot preserve all uint16 values. Round exactly like
// np.rint on the arithmetic mean (nearest, ties to even).
static NSString *const shaderSource =
    @"#include <metal_stdlib>\n"
     "using namespace metal;\n"
     "struct Dimensions { uint channels; uint height; uint width; };\n"
     "template<typename T>\n"
     "inline void reduce_pixel(device const T *src, device T *dst,\n"
     "                         constant Dimensions &d, uint3 p) {\n"
     "    uint ow = d.width / 2, oh = d.height / 2;\n"
     "    if (p.x >= ow || p.y >= oh || p.z >= d.channels) return;\n"
     "    ulong i = (ulong(p.z) * d.height + ulong(p.y) * 2) * d.width\n"
     "              + ulong(p.x) * 2;\n"
     "    uint sum = uint(src[i]) + uint(src[i + 1])\n"
     "             + uint(src[i + d.width]) + uint(src[i + d.width + 1]);\n"
     "    uint q = sum >> 2, r = sum & 3;\n"
     "    q += uint(r > 2 || (r == 2 && (q & 1) != 0));\n"
     "    ulong o = (ulong(p.z) * oh + p.y) * ow + p.x;\n"
     "    dst[o] = T(q);\n"
     "}\n"
     "kernel void reduce_u8(device const uchar *src [[buffer(0)]],\n"
     "                      device uchar *dst [[buffer(1)]],\n"
     "                      constant Dimensions &d [[buffer(2)]],\n"
     "                      uint3 p [[thread_position_in_grid]]) {\n"
     "    reduce_pixel(src, dst, d, p);\n"
     "}\n"
     "kernel void reduce_u16(device const ushort *src [[buffer(0)]],\n"
     "                       device ushort *dst [[buffer(1)]],\n"
     "                       constant Dimensions &d [[buffer(2)]],\n"
     "                       uint3 p [[thread_position_in_grid]]) {\n"
     "    reduce_pixel(src, dst, d, p);\n"
     "}\n";

static int fail(char *error, size_t capacity, NSString *message) {
    if (error != NULL && capacity > 0) {
        const char *utf8 = [message UTF8String];
        snprintf(error, capacity, "%s", utf8 != NULL ? utf8 : "Metal error");
    }
    return 1;
}

static void initializeMetal(void) {
    dispatch_once(&initializationOnce, ^{
        @try {
#if !(defined(__arm64__) || defined(__aarch64__))
            initializationError = @"Metal pyramid beta requires native arm64 macOS";
            return;
#else
            if (@available(macOS 11.0, *)) {
                device = MTLCreateSystemDefaultDevice();
                if (device == nil) {
                    initializationError = @"No Metal device is available";
                    return;
                }
                if (![device hasUnifiedMemory]) {
                    initializationError = @"Metal pyramid beta requires unified memory";
                    return;
                }
                queue = [device newCommandQueue];
                if (queue == nil) {
                    initializationError = @"Could not create Metal command queue";
                    return;
                }
                queue.label = @"nd2wsi experimental pyramid";
                NSError *error = nil;
                MTLCompileOptions *options = [MTLCompileOptions new];
                // Integer reduction needs no floating-point shortcuts.
                options.fastMathEnabled = NO;
                id<MTLLibrary> library = [device newLibraryWithSource:shaderSource
                                                            options:options
                                                              error:&error];
                if (library == nil) {
                    initializationError = [NSString stringWithFormat:
                        @"Metal shader compilation failed: %@", error.localizedDescription];
                    return;
                }
                id<MTLFunction> function8 = [library newFunctionWithName:@"reduce_u8"];
                id<MTLFunction> function16 = [library newFunctionWithName:@"reduce_u16"];
                if (function8 == nil || function16 == nil) {
                    initializationError = @"Metal reducer kernel is missing";
                    return;
                }
                pipeline8 = [device newComputePipelineStateWithFunction:function8 error:&error];
                if (pipeline8 == nil) {
                    initializationError = [NSString stringWithFormat:
                        @"Metal uint8 pipeline failed: %@", error.localizedDescription];
                    return;
                }
                pipeline16 = [device newComputePipelineStateWithFunction:function16 error:&error];
                if (pipeline16 == nil) {
                    initializationError = [NSString stringWithFormat:
                        @"Metal uint16 pipeline failed: %@", error.localizedDescription];
                    return;
                }
            } else {
                initializationError = @"Metal pyramid beta requires macOS 11 or newer";
            }
#endif
        } @catch (NSException *exception) {
            initializationError = [NSString stringWithFormat:
                @"Metal initialization exception: %@", exception.reason];
        }
    });
}

static BOOL multiplySize(size_t a, size_t b, size_t *result) {
    if (b != 0 && a > SIZE_MAX / b) return NO;
    *result = a * b;
    return YES;
}

static int runReduction(const void *input, size_t inputLength,
                        void *output, size_t outputLength,
                        ReductionDimensions dimensions, uint32_t bits,
                        double *gpuSeconds, char *error, size_t errorLength) {
    // No deallocator is installed: neither Metal nor this bridge owns mmap.
    id<MTLBuffer> source = [device newBufferWithBytesNoCopy:(void *)input
                                                  length:inputLength
                                                 options:MTLResourceStorageModeShared
                                             deallocator:nil];
    if (source == nil) return fail(error, errorLength, @"Could not wrap input mmap in Metal buffer");
    id<MTLBuffer> destination = [device newBufferWithBytesNoCopy:output
                                                       length:outputLength
                                                      options:MTLResourceStorageModeShared
                                                  deallocator:nil];
    if (destination == nil) return fail(error, errorLength, @"Could not wrap output mmap in Metal buffer");
    source.label = @"nd2wsi pyramid input (caller mmap)";
    destination.label = @"nd2wsi pyramid output (caller mmap)";
    id<MTLComputePipelineState> pipeline = bits == 8 ? pipeline8 : pipeline16;
    id<MTLCommandBuffer> command = [queue commandBuffer];
    if (command == nil) return fail(error, errorLength, @"Could not create Metal command buffer");
    command.label = @"nd2wsi exact 2x2 reduction";
    id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
    if (encoder == nil) return fail(error, errorLength, @"Could not create Metal compute encoder");
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:source offset:0 atIndex:0];
    [encoder setBuffer:destination offset:0 atIndex:1];
    [encoder setBytes:&dimensions length:sizeof(dimensions) atIndex:2];
    NSUInteger outputWidth = dimensions.width / 2;
    NSUInteger outputHeight = dimensions.height / 2;
    NSUInteger groupWidth = MIN(outputWidth, pipeline.threadExecutionWidth);
    NSUInteger groupHeight = MIN(outputHeight, MIN((NSUInteger)8,
        pipeline.maxTotalThreadsPerThreadgroup / groupWidth));
    [encoder dispatchThreads:MTLSizeMake(outputWidth, outputHeight, dimensions.channels)
       threadsPerThreadgroup:MTLSizeMake(groupWidth, groupHeight, 1)];
    [encoder endEncoding];
    [command commit];
    // This is also the CPU/GPU ownership hand-off for shared storage. No
    // synchronizeResource blit (needed for managed storage) belongs here.
    [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted) {
        return fail(error, errorLength, [NSString stringWithFormat:
            @"Metal reduction failed (status %lu): %@", (unsigned long)command.status,
            command.error.localizedDescription ?: @"unknown GPU error"]);
    }
    if (gpuSeconds != NULL && command.GPUEndTime >= command.GPUStartTime) {
        *gpuSeconds = command.GPUEndTime - command.GPUStartTime;
    }
    return 0;
}

// Returns zero on success. Nonzero leaves a bounded, NUL-terminated error if
// supplied. Both lengths are allocation capacities, NOT logical image bytes.
ND2WSI_EXPORT int nd2wsi_metal_reduce(
    const void *input, size_t input_len, void *output, size_t output_len,
    uint32_t channels, uint32_t height, uint32_t width, uint32_t bits,
    double *gpu_seconds, char *error, size_t error_len) {
    @autoreleasepool {
        if (gpu_seconds != NULL) *gpu_seconds = 0.0;
        if (error != NULL && error_len > 0) error[0] = '\0';
        if (input == NULL || output == NULL)
            return fail(error, error_len, @"Input and output pointers must be non-null");
        if (channels == 0 || height == 0 || width == 0 || (height & 1) || (width & 1))
            return fail(error, error_len, @"Expected positive channels and positive even height/width");
        if (bits != 8 && bits != 16)
            return fail(error, error_len, @"Only native uint8 and uint16 CYX data are supported");
        const size_t page = (size_t)getpagesize();
        if (page == 0 || (uintptr_t)input % page || (uintptr_t)output % page ||
            input_len == 0 || output_len == 0 || input_len % page || output_len % page)
            return fail(error, error_len, @"Both mmap addresses and allocation lengths must be page-aligned");
        size_t pixels = 0, requiredInput = 0;
        if (!multiplySize((size_t)channels, (size_t)height, &pixels) ||
            !multiplySize(pixels, (size_t)width, &pixels) ||
            !multiplySize(pixels, (size_t)(bits / 8), &requiredInput))
            return fail(error, error_len, @"Image dimensions overflow size_t");
        if (input_len < requiredInput || output_len < requiredInput / 4)
            return fail(error, error_len, @"mmap allocation is too small for the requested image");
        uintptr_t inputStart = (uintptr_t)input, outputStart = (uintptr_t)output;
        if (input_len > UINTPTR_MAX - inputStart || output_len > UINTPTR_MAX - outputStart)
            return fail(error, error_len, @"mmap address range overflows uintptr_t");
        if (inputStart < outputStart + output_len && outputStart < inputStart + input_len)
            return fail(error, error_len, @"Input and output mmap allocations must not overlap");
        initializeMetal();
        if (initializationError != nil) return fail(error, error_len, initializationError);
        if (input_len > device.maxBufferLength || output_len > device.maxBufferLength)
            return fail(error, error_len, @"mmap allocation exceeds device maxBufferLength; use smaller blocks");
        if (pthread_mutex_lock(&reductionMutex) != 0)
            return fail(error, error_len, @"Could not lock Metal reducer");
        int result = 1;
        @try {
            ReductionDimensions dimensions = {channels, height, width};
            result = runReduction(input, input_len, output, output_len,
                                  dimensions, bits, gpu_seconds, error, error_len);
        } @catch (NSException *exception) {
            result = fail(error, error_len, [NSString stringWithFormat:
                @"Metal reduction exception: %@", exception.reason]);
        } @finally {
            pthread_mutex_unlock(&reductionMutex);
        }
        // ARC locals and autoreleased command/buffer references are gone before
        // returning to Python, so the caller may now close either allocation.
        return result;
    }
}

// A successfully serialized JSON response returns zero even if unsupported.
// A missing/undersized output buffer returns nonzero (never truncated JSON).
ND2WSI_EXPORT int nd2wsi_metal_info(char *out, size_t capacity) {
    @autoreleasepool {
        if (out == NULL || capacity == 0) return 1;
        out[0] = '\0';
        initializeMetal();
        @try {
            NSMutableDictionary *info = [@{
                @"supported": initializationError == nil ? @YES : @NO,
                @"device": device.name ?: @"",
                @"unified_memory": device != nil && device.hasUnifiedMemory ? @YES : @NO,
                @"page_size": @((size_t)getpagesize()),
                @"max_buffer_length": @(device != nil ? device.maxBufferLength : 0),
                @"recommended_max_working_set_size": @(device != nil ? device.recommendedMaxWorkingSetSize : 0),
                @"reducer": @"exact_uint_2x2_ties_to_even",
                @"abi_version": @1
            } mutableCopy];
            if (initializationError != nil) info[@"error"] = initializationError;
            NSData *json = [NSJSONSerialization dataWithJSONObject:info options:0 error:nil];
            if (json == nil || json.length >= capacity) return 1;
            memcpy(out, json.bytes, json.length);
            out[json.length] = '\0';
            return 0;
        } @catch (NSException *exception) {
            return 1;
        }
    }
}
