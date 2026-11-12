package demo;
import org.junit.jupiter.api.Test;
import jakarta.ws.rs.BadRequestException;
import static org.junit.jupiter.api.Assertions.*;
class CheckoutContractTest {
 private final CheckoutResource resource = new CheckoutResource();
 @Test void missingCouponIsFullPrice() { assertEquals(10000, resource.checkout(new CheckoutResource.Order(10000,null)).totalCents()); }
 @Test void blankCouponIsFullPrice() { assertEquals(10000, resource.checkout(new CheckoutResource.Order(10000,"  ")).totalCents()); }
 @Test void unknownCouponIsFullPrice() { assertEquals(10000, resource.checkout(new CheckoutResource.Order(10000,"NOPE")).totalCents()); }
 @Test void discountStillWorks() { assertEquals(9000, resource.checkout(new CheckoutResource.Order(10000," save10 ")).totalCents()); }
 @Test void zeroIsValid() { assertEquals(0, resource.checkout(new CheckoutResource.Order(0,null)).totalCents()); }
 @Test void invalidSubtotalRejected() { assertThrows(BadRequestException.class, () -> resource.checkout(new CheckoutResource.Order(-1,null))); }
 @Test void upperBoundRejected() { assertThrows(BadRequestException.class, () -> resource.checkout(new CheckoutResource.Order(1000001,null))); }
 @Test void currencyUnchanged() { assertEquals("EUR", resource.checkout(new CheckoutResource.Order(100,null)).currency()); }
}
