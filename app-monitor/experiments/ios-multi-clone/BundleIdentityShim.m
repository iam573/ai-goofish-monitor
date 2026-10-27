#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/runtime.h>
#import <string.h>

static NSString *const GoofishOriginalBundleIdentifier = @"com.taobao.fleamarket";

@interface NSBundle (GoofishIdentityShim)
- (NSString *)gfm_bundleIdentifier;
@end

@implementation NSBundle (GoofishIdentityShim)

+ (void)load {
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        Class cls = NSBundle.class;
        method_exchangeImplementations(class_getInstanceMethod(cls, @selector(bundleIdentifier)),
                                       class_getInstanceMethod(cls, @selector(gfm_bundleIdentifier)));
    });
}

- (BOOL)gfm_isMainApplicationBundle {
    return self == NSBundle.mainBundle;
}

- (NSString *)gfm_bundleIdentifier {
    if ([self gfm_isMainApplicationBundle]) {
        Dl_info callerInfo = {0};
        if (dladdr(__builtin_return_address(0), &callerInfo) != 0 && callerInfo.dli_fname != NULL) {
            const char *image = callerInfo.dli_fname;
            if (strstr(image, "/UIKitCore.framework/") != NULL ||
                strstr(image, "/CoreUI.framework/") != NULL ||
                strstr(image, "/SwiftUI.framework/") != NULL) {
                return [self gfm_bundleIdentifier];
            }
        }
        return GoofishOriginalBundleIdentifier;
    }
    return [self gfm_bundleIdentifier];
}

@end
