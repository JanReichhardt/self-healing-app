package demo;

import jakarta.ws.rs.*;
import jakarta.ws.rs.core.MediaType;

@Path("/checkout")
@Consumes(MediaType.APPLICATION_JSON)
@Produces(MediaType.APPLICATION_JSON)
public class CheckoutResource {
    public record Order(long subtotalCents, String coupon) {}
    public record Receipt(long totalCents, String currency) {}

    @POST
    public Receipt checkout(Order order) {
        if (order == null || order.subtotalCents() < 0 || order.subtotalCents() > 1_000_000) {
            throw new BadRequestException("subtotalCents must be between 0 and 1000000");
        }
        // Deliberate incident: optional coupon is dereferenced without a null check.
        String coupon = order.coupon().trim();
        long total = "SAVE10".equalsIgnoreCase(coupon)
            ? order.subtotalCents() * 90 / 100 : order.subtotalCents();
        return new Receipt(total, "EUR");
    }
}
