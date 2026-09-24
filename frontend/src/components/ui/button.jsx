import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground shadow hover:bg-primary/90 active:scale-[0.99]",
        primary:
          "bg-primary text-primary-foreground shadow hover:bg-primary/90 active:scale-[0.99]",
        destructive:
          "bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90 active:scale-[0.99]",
        outline:
          "border border-input bg-background text-foreground shadow-sm hover:bg-accent hover:text-accent-foreground active:scale-[0.99]",
        secondary:
          "border border-input bg-secondary text-secondary-foreground shadow-sm hover:bg-secondary/80 active:scale-[0.99]",
        ghost:
          "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        link:
          "text-primary underline-offset-4 hover:underline",
        subtle:
          "bg-accent text-accent-foreground hover:bg-accent/80",
        segmented:
          "flex-1 border border-transparent bg-transparent text-muted-foreground hover:bg-muted/40 hover:text-foreground font-medium shadow-none transition-all data-[active=true]:bg-card data-[active=true]:text-primary data-[active=true]:border-border data-[active=true]:shadow-xs active:scale-[0.99]",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 rounded-md px-3 text-xs",
        lg: "h-10 rounded-md px-6 text-base",
        icon: "h-9 w-9 p-0",
        "icon-sm": "h-7 w-7 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

function renderIcon(icon, { size = 16, className } = {}) {
  if (!icon) return null;
  if (React.isValidElement(icon)) {
    return React.cloneElement(icon, {
      className: cn("shrink-0", icon.props?.className, className),
      size: icon.props?.size || size,
    });
  }
  if (typeof icon === "function" || typeof icon === "object") {
    const IconComp = icon;
    return <IconComp size={size} className={cn("shrink-0", className)} />;
  }
  return null;
}

const Button = React.forwardRef(
  (
    {
      className,
      variant,
      size,
      asChild = false,
      icon,
      iconPosition = "left",
      iconClassName,
      iconSize = 16,
      loading = false,
      loadingText,
      active,
      disabled,
      children,
      ...props
    },
    ref
  ) => {
    if (asChild) {
      return (
        <Slot
          data-slot="button"
          className={cn(buttonVariants({ variant, size, className }))}
          ref={ref}
          {...props}
        >
          {children}
        </Slot>
      );
    }

    const isInteractiveDisabled = Boolean(disabled || loading);
    const computedActive =
      active !== undefined ? active : (props["data-active"] ?? false);

    const renderedIcon = loading ? (
      <Loader2
        size={iconSize}
        className={cn("animate-spin shrink-0", iconClassName)}
      />
    ) : (
      renderIcon(icon, { size: iconSize, className: iconClassName })
    );

    return (
      <button
        ref={ref}
        data-slot="button"
        data-active={computedActive ? "true" : undefined}
        disabled={isInteractiveDisabled}
        className={cn(
          buttonVariants({ variant, size, className }),
          computedActive && "active",
          className
        )}
        {...props}
      >
        {renderedIcon && iconPosition === "left" && renderedIcon}
        {loading && loadingText ? loadingText : children}
        {renderedIcon && iconPosition === "right" && !loading && renderedIcon}
      </button>
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
